import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { setAuth, isKavachioAdmin, isTenantAdmin } from "../auth";
import { armLoginNotice } from "../api/notifications";
import KavachioLogo from "../components/KavachioLogo";
import { PasswordInput } from "../components/ui/PasswordInput";

type View = "signin" | "forgotEmail" | "forgotSent";

export default function Login() {
  const nav = useNavigate();
  const [view, setView] = useState<View>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [resetEmail, setResetEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null); setBusy(true);
    try {
      const { data } = await api.post("/auth/login", { email, password });
      setAuth(data);
      // Let the shell show its sign-in message once for this session. Armed for
      // every role; <PlatformNotificationCard/> is what decides it has anything
      // to say (platform admins only, and only when something is waiting).
      armLoginNotice();
      // Kavachio platform admins have their own screen (the admin dashboard —
      // Tenants + Data Mapping queue) and don't run tenant onboarding, so route
      // them straight there instead of the tenant Home dashboard.
      if (isKavachioAdmin()) {
        nav("/admin/dashboard");
        return;
      }
      // Only tenant admins run the org / carrier / Bordereau setup, so only they
      // are routed through Welcome. Operators go straight to the dashboard, which
      // shows a "not configured — ask your admin" notice when setup is pending.
      try {
        const onboarding = await api.get("/onboarding/status", { params: { mga: data.mga } });
        nav(onboarding.data?.needs_onboarding && isTenantAdmin() ? "/welcome" : "/home");
      } catch {
        nav("/home");
      }
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Login failed.");
    } finally { setBusy(false); }
  }

  function openForgot() {
    setResetEmail(email);          // carry over whatever was typed
    setErr(null);
    setView("forgotEmail");
  }
  async function sendReset(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      // Backend emails a reset link if the account exists. Response is the same
      // either way (no user enumeration), so we don't branch on the result.
      await api.post("/auth/forgot", { email: resetEmail });
    } catch {
      /* swallow — still show the neutral confirmation */
    } finally {
      setBusy(false);
      setView("forgotSent");
    }
  }
  function backToSignin() {
    setView("signin");
    setErr(null);
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
      {/* faint grid pattern, masked toward the centre */}
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
        {/* Brand */}
        <div className="flex items-center gap-3 mb-2">
          <KavachioLogo size={38} style={{ filter: "drop-shadow(0 4px 10px rgba(7,114,130,.45))" }} />
          <h1 className="m-0 text-[23px] font-bold tracking-[.2px]"
            style={{ fontFamily: "'Montserrat', sans-serif", color: "#0E1320" }}>
            Kavachio
          </h1>
        </div>

        {/* ---------------- SIGN IN ---------------- */}
        {view === "signin" && (
          <>
            <p className="mb-6 text-sm" style={{ color: "#566071" }}>Bordereau &amp; Contract Validation</p>
            <form onSubmit={submit}>
              <div className="mb-4">
                <label className="mb-1.5 block text-xs font-semibold" style={{ color: "#566071" }}>Email</label>
                <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus
                  placeholder="you@company.com" className={inputCls} style={inputStyle} />
              </div>
              <div className="mb-4">
                <label className="mb-1.5 block text-xs font-semibold" style={{ color: "#566071" }}>Password</label>
                <PasswordInput value={password} onChange={(e) => setPassword(e.target.value)} required
                  placeholder="••••••••••" className={inputCls} style={inputStyle} />
              </div>
              <div className="-mt-1 mb-1.5 text-right text-[12.5px]">
                <a href="#" onClick={(e) => { e.preventDefault(); openForgot(); }} style={{ color: "#077282" }}>
                  Forgot Password?
                </a>
              </div>
              {err && <div className="mb-2 text-sm" style={{ color: "#D32F45" }}>{err}</div>}
              <button type="submit" disabled={busy} className={priBtn} style={priShadow}>
                Sign In
              </button>
            </form>
            <div className="mt-5 text-center text-[11.5px]" style={{ color: "#8B93A2" }}>
              You'll land on the right home screen for your role.
            </div>
          </>
        )}

        {/* ---------------- FORGOT · STEP 1 (enter email) ---------------- */}
        {view === "forgotEmail" && (
          <>
            <p className="mb-6 text-sm" style={{ color: "#566071" }}>
              Reset your password — enter your account email and we'll send a reset link.
            </p>
            <form onSubmit={sendReset}>
              <div className="mb-4">
                <label className="mb-1.5 block text-xs font-semibold" style={{ color: "#566071" }}>Email</label>
                <input type="email" value={resetEmail} onChange={(e) => setResetEmail(e.target.value)} required autoFocus
                  placeholder="you@company.com" className={inputCls} style={inputStyle} />
              </div>
              <button type="submit" disabled={busy} className={priBtn} style={priShadow}>
                Send Reset Link
              </button>
            </form>
            <div className="mt-5 text-center text-[12.5px]">
              <a href="#" onClick={(e) => { e.preventDefault(); backToSignin(); }} style={{ color: "#077282" }}>
                ← Back to Sign In
              </a>
            </div>
          </>
        )}

        {/* ---------------- FORGOT · STEP 2 (confirmation) ---------------- */}
        {view === "forgotSent" && (
          <div className="text-center">
            <div className="mx-auto mt-1.5 mb-3.5 grid h-[52px] w-[52px] place-items-center rounded-full"
              style={{ background: "#E4F5EC", color: "#0E9F6E" }}>
              <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
                <path d="M5 12l5 5L20 7" />
              </svg>
            </div>
            <h3 className="m-0 mb-1.5 text-[18px] font-semibold"
              style={{ fontFamily: "'Montserrat', sans-serif", color: "#0E1320" }}>
              Check Your Inbox
            </h3>
            <p className="mb-[18px] text-sm" style={{ color: "#566071" }}>
              If an account exists for <b>{resetEmail || "that email"}</b>, a reset link is on its way.
              The link expires in 30 minutes.
            </p>
            <button onClick={backToSignin}
              className="flex w-full items-center justify-center rounded-lg border py-3 text-sm font-semibold transition hover:bg-[#F7F8FB]"
              style={{ borderColor: "#D2D7E0", color: "#0E1320" }}>
              Back to Sign In
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
