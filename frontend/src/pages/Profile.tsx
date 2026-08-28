import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { getUser, normalizeRole, ROLE_LABEL, setUser } from "../auth";
import { PasswordInput } from "../components/ui/PasswordInput";

// Password policy — kept in sync with the backend (POST /auth/change-password).
const RULES: { label: string; test: (p: string) => boolean }[] = [
  { label: "At least 8 characters", test: p => p.length >= 8 },
  { label: "One uppercase letter (A–Z)", test: p => /[A-Z]/.test(p) },
  { label: "One lowercase letter (a–z)", test: p => /[a-z]/.test(p) },
  { label: "One number (0–9)", test: p => /[0-9]/.test(p) },
  { label: "One special character", test: p => /[^A-Za-z0-9]/.test(p) },
];

export default function Profile() {
  const nav = useNavigate();
  const user = getUser();

  // --- profile (name) -------------------------------------------------------
  const [fullName, setFullName] = useState(user?.full_name ?? "");
  const [savingName, setSavingName] = useState(false);
  const [nameMsg, setNameMsg] = useState<string | null>(null);
  const [nameErr, setNameErr] = useState<string | null>(null);

  // --- change password ------------------------------------------------------
  const [current, setCurrent] = useState("");
  const [pwd, setPwd] = useState("");
  const [confirm, setConfirm] = useState("");
  const [savingPwd, setSavingPwd] = useState(false);
  const [pwdMsg, setPwdMsg] = useState<string | null>(null);
  const [pwdErr, setPwdErr] = useState<string | null>(null);

  if (!user) {
    // RequireAuth guards this route, but stay defensive.
    nav("/login");
    return null;
  }

  const roleLabel = ROLE_LABEL[normalizeRole(user.role)];
  const nameChanged = fullName.trim() !== (user.full_name ?? "").trim();

  const met = RULES.map(r => r.test(pwd));
  const allMet = met.every(Boolean);
  const matches = pwd.length > 0 && pwd === confirm;
  const canChangePwd = current.length > 0 && allMet && matches && !savingPwd;

  async function saveName() {
    const name = fullName.trim();
    if (!name) { setNameErr("Full name is required."); return; }
    setNameErr(null); setNameMsg(null); setSavingName(true);
    try {
      await api.put(`/users/${user!.id}/profile`, { full_name: name });
      // Reflect the new name in the sidebar (and everywhere getUser() is read).
      setUser({ ...user!, full_name: name });
      setNameMsg("Saved");
    } catch (e: any) {
      setNameErr(e?.response?.data?.detail ?? "Couldn't save your profile.");
    } finally { setSavingName(false); }
  }

  async function changePassword() {
    setPwdErr(null); setPwdMsg(null);
    if (!allMet) { setPwdErr("New password doesn't meet all the requirements."); return; }
    if (!matches) { setPwdErr("New passwords don't match."); return; }
    setSavingPwd(true);
    try {
      await api.post("/auth/change-password", {
        user_id: user!.id,
        current_password: current,
        new_password: pwd,
      });
      setPwdMsg("Your password has been changed.");
      setCurrent(""); setPwd(""); setConfirm("");
    } catch (e: any) {
      setPwdErr(e?.response?.data?.detail ?? "Couldn't change your password.");
    } finally { setSavingPwd(false); }
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>My Account</h2>
            <p>Your personal details and sign-in security.</p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav(-1)}>← Back</button>
          </div>
        </div>

        <div className="grid g-2">
          {/* Account details */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Account Details</h3>
            <div className="field">
              <label>Full name</label>
              <input value={fullName} autoFocus placeholder="Your name"
                onChange={e => { setFullName(e.target.value); setNameMsg(null); }} />
            </div>
            <div className="field">
              <label>Email</label>
              <input className="ro" value={user.email} readOnly />
              <div className="hint">Your email is your sign-in and can't be changed here.</div>
            </div>
            <div className="row2">
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Role</label>
                <input className="ro" value={roleLabel} readOnly />
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Organization</label>
                <input className="ro" value={user.mga ?? "—"} readOnly />
              </div>
            </div>

            <div style={{ marginTop: 18, display: "flex", alignItems: "center", gap: 12 }}>
              <button className="btn pri" onClick={saveName} disabled={savingName || !nameChanged}>
                Save Changes
              </button>
              {nameMsg && <span className="muted" style={{ fontSize: 13, color: "var(--p-ok-ink)" }}>{nameMsg}</span>}
              {nameErr && <span style={{ fontSize: 13, color: "var(--p-crit)" }}>{nameErr}</span>}
            </div>
          </div>

          {/* Change password */}
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Change Password</h3>
            <div className="field">
              <label>Current password</label>
              {/* readOnly-until-focus + autoComplete off stops Chrome from
                  auto-filling the saved login password into this field on load. */}
              <PasswordInput value={current} placeholder="Enter current password"
                autoComplete="off" readOnly
                onFocus={e => e.currentTarget.removeAttribute("readonly")}
                onChange={e => { setCurrent(e.target.value); setPwdMsg(null); }} />
            </div>
            <div className="field">
              <label>New password</label>
              <PasswordInput value={pwd} placeholder="Create a strong password"
                autoComplete="new-password"
                onChange={e => { setPwd(e.target.value); setPwdMsg(null); }} />
            </div>

            {/* live requirements checklist */}
            <ul style={{ listStyle: "none", margin: "0 0 14px", padding: 0, display: "grid", gap: 4 }}>
              {RULES.map((r, i) => {
                const ok = met[i];
                return (
                  <li key={r.label} style={{
                    display: "flex", alignItems: "center", gap: 8, fontSize: 12,
                    color: ok ? "var(--p-ok-ink)" : "var(--p-faint)",
                  }}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6">
                      {ok ? <path d="M5 12l5 5L20 7" /> : <circle cx="12" cy="12" r="9" strokeWidth={1.8} />}
                    </svg>
                    {r.label}
                  </li>
                );
              })}
            </ul>

            <div className="field" style={{ marginBottom: 0 }}>
              <label>Confirm new password</label>
              <PasswordInput value={confirm} placeholder="Re-enter new password"
                autoComplete="new-password"
                onChange={e => { setConfirm(e.target.value); setPwdMsg(null); }} />
              {confirm.length > 0 && !matches && (
                <div className="hint" style={{ color: "var(--p-crit)" }}>Passwords don't match.</div>
              )}
            </div>

            <div style={{ marginTop: 18, display: "flex", alignItems: "center", gap: 12 }}>
              <button className="btn pri" onClick={changePassword} disabled={!canChangePwd}>
                Update Password
              </button>
              {pwdMsg && <span className="muted" style={{ fontSize: 13, color: "var(--p-ok-ink)" }}>{pwdMsg}</span>}
              {pwdErr && <span style={{ fontSize: 13, color: "var(--p-crit)" }}>{pwdErr}</span>}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
