"""Route handlers owned by auth-service. Extracted from: app_routes.py."""
from __future__ import annotations
from fastapi import APIRouter
from common_app_routes import *

router = APIRouter()


@router.post("/auth/login")
def auth_login(body: LoginBody, request: Request):
    """Authenticate user and issue a short-lived access token + a 7-day refresh
    token (MULTITENANCY_AUTH_CONCEPT.md §5.1). The client stores both and sends
    the access token as `Authorization: Bearer <jwt>` on every call.

    Unknown email and wrong password return the SAME 401 (no user enumeration).
    Auto-provision-on-login was removed — creating users is an explicit admin
    action via POST /users."""
    from auth_utils import hash_password, verify_password, _is_bcrypt
    from auth_tokens import mint_access_token, mint_refresh_token
    from auth_deps import normalize_role
    email = (body.email or "").strip().lower()
    # Throttle brute force before touching the DB.
    _client_ip = request.client.host if request.client else "unknown"
    _rate_limit_login(f"ip:{_client_ip}", f"email:{email}")
    with SessionLocal() as s:
        u = s.query(AppUser).filter(AppUser.email == email).first()
        if not u or not u.password or not verify_password(body.password, u.password):
            raise HTTPException(401, "invalid credentials")
        if (u.status or "active") != "active":
            raise HTTPException(403, "user disabled")
        # Auto-upgrade legacy plain-text password to bcrypt on successful login.
        if not _is_bcrypt(u.password):
            u.password = hash_password(body.password)
            s.commit()
        # Record the successful sign-in time (shown on the Users & Roles screen).
        u.last_login_at = datetime.utcnow()
        s.commit()
        # `mga` is the tenant_name the frontend still keys requests on.
        t = s.query(Tenant).filter(Tenant.id == u.tenant_id).first() if u.tenant_id else None
        role = normalize_role(u.role)
        return {
            "access_token":  mint_access_token(u.id, u.tenant_id, role),
            "refresh_token": mint_refresh_token(u.id),
            "token_type":    "bearer",
            "id": u.id, "email": u.email, "full_name": u.full_name,
            "tenant_id": u.tenant_id,
            "role": role, "mga": t.tenant_name if t else None,
        }


@router.post("/auth/refresh")
def auth_refresh(body: RefreshBody):
    """Mint a fresh access token from a valid refresh token (§5.3). Role and
    tenant are re-read from the DB so mid-session changes take effect. The
    refresh token is NOT rotated: it keeps its original 7-day expiry, so the
    session ends 7 days after login and the client must then re-login."""
    from auth_tokens import decode_refresh_token, mint_access_token
    from auth_deps import normalize_role
    from jose import JWTError
    try:
        claims = decode_refresh_token(body.refresh_token)
    except JWTError:
        raise HTTPException(401, "invalid or expired refresh token")
    with SessionLocal() as s:
        u = s.get(AppUser, int(claims["sub"]))
        if not u or (u.status or "active") != "active":
            raise HTTPException(401, "user not found or disabled")
        return {
            "access_token": mint_access_token(u.id, u.tenant_id, normalize_role(u.role)),
            "token_type":   "bearer",
        }


@router.post("/auth/logout")
def auth_logout():
    """Stateless logout. Tokens are self-contained (no server-side session), so
    the client simply discards them. Kept as an endpoint so revocable refresh
    records can be wired in later without a frontend change."""
    return {"ok": True}


@router.post("/auth/forgot")
def auth_forgot(body: ForgotBody):
    """Start a password reset: generate a 30-minute token, email the user a
    reset link. Always returns 200 regardless of whether the email matches an
    account (no user enumeration)."""
    import os
    import secrets
    email = (body.email or "").strip().lower()
    with SessionLocal() as s:
        u = s.query(AppUser).filter(AppUser.email == email).first()
        if u:
            token = secrets.token_urlsafe(32)
            u.reset_token = token
            u.reset_token_expires = datetime.now(timezone.utc) + timedelta(minutes=30)
            s.commit()
            base = os.getenv("APP_BASE_URL", "http://localhost:5173").rstrip("/")
            link = f"{base}/reset?token={token}"
            name = (u.full_name or "").strip()
            try:
                from email_utils import send_email, reset_email_html
                send_email(
                    email, "Reset your Kavachio password",
                    reset_email_html(link, name),
                    text=(f"Hi {name}, " if name else "")
                    + f"reset your Kavachio password (expires in 30 minutes): {link}",
                )
            except Exception as e:
                import logging
                logging.getLogger("bdx.email").warning(
                    "password-reset email to %s failed: %s", email, e)
    return {"ok": True}


@router.get("/auth/reset/validate")
def auth_reset_validate(token: str):
    """Check whether a reset token is still valid (exists + not expired) WITHOUT
    consuming it — so the reset page can show an 'expired' message on load
    instead of the set-password form."""
    with SessionLocal() as s:
        u = s.query(AppUser).filter(AppUser.reset_token == token).first() if token else None
        exp = u.reset_token_expires if u else None
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        valid = bool(u and exp and exp >= datetime.now(timezone.utc))
        return {"valid": valid}


@router.post("/auth/reset")
def auth_reset(body: ResetBody):
    """Complete a password reset: validate the token (exists + not expired),
    set the new bcrypt password, and clear the token."""
    import re
    from auth_utils import hash_password
    pw = body.password or ""
    if not (len(pw) >= 8 and re.search(r"[A-Z]", pw) and re.search(r"[a-z]", pw)
            and re.search(r"\d", pw) and re.search(r"[^A-Za-z0-9]", pw)):
        raise HTTPException(
            400,
            "Password must be at least 8 characters and include an uppercase letter, "
            "a lowercase letter, a number, and a special character.",
        )
    with SessionLocal() as s:
        u = s.query(AppUser).filter(AppUser.reset_token == body.token).first()
        exp = u.reset_token_expires if u else None
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if not u or exp is None or exp < datetime.now(timezone.utc):
            raise HTTPException(400, "This reset link is invalid or has expired.")
        u.password = hash_password(body.password)
        u.reset_token = None
        u.reset_token_expires = None
        u.status = "active"
        s.commit()
        _log(_tenant_name(s, u.tenant_id), None, "password_reset", target=str(u.id))
        return {"ok": True}


@router.post("/auth/change-password")
def auth_change_password(body: ChangePasswordBody):
    """Change the signed-in user's own password.

    Verifies the current password, enforces the same policy as /auth/reset,
    and stores the new bcrypt hash. (No auth token in this POC — the caller
    passes its own user_id; the current-password check is what authorizes it.)
    """
    import re
    from auth_utils import hash_password, verify_password
    new = body.new_password or ""
    if not (len(new) >= 8 and re.search(r"[A-Z]", new) and re.search(r"[a-z]", new)
            and re.search(r"\d", new) and re.search(r"[^A-Za-z0-9]", new)):
        raise HTTPException(
            400,
            "Password must be at least 8 characters and include an uppercase letter, "
            "a lowercase letter, a number, and a special character.",
        )
    with SessionLocal() as s:
        u = s.get(AppUser, body.user_id)
        if not u:
            raise HTTPException(404, "user not found")
        # A user with a password set must prove they know the current one.
        if u.password and not verify_password(body.current_password, u.password):
            raise HTTPException(400, "Your current password is incorrect.")
        u.password = hash_password(body.new_password)
        s.commit()
        _log(_tenant_name(s, u.tenant_id), None, "password_changed", target=str(u.id))
        return {"ok": True}


@router.get("/users")
def users_list(mga: str, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        return [_user_dict(u, mga) for u in
                s.query(AppUser).filter(AppUser.tenant_id == tid)
                .order_by(AppUser.email).all()]


@router.post("/users")
def users_create(mga: str, body: UserBody,
                 principal: Principal = Depends(require_role("tenant_admin"))):
    from auth_utils import hash_password
    with SessionLocal() as s:
        if s.query(AppUser).filter(AppUser.email == body.email.lower()).first():
            raise HTTPException(409, "email already exists")
        hashed = hash_password(body.password) if body.password else None
        u = AppUser(email=body.email.strip().lower(),
                    full_name=body.full_name, role=body.role or "ops",
                    status=body.status or "active", password=hashed,
                    tenant_id=resolve_tenant_id(s, principal, mga))
        s.add(u); s.commit(); s.refresh(u)
        _log(mga, None, "user_created", target=str(u.id))
        return _user_dict(u, mga)


@router.put("/users/{user_id}")
def users_update(user_id: int, body: UserBody,
                 principal: Principal = Depends(require_role("tenant_admin"))):
    from auth_utils import hash_password
    with SessionLocal() as s:
        u = s.get(AppUser, user_id)
        if not u:
            raise HTTPException(404, "user not found")
        assert_tenant_owns(principal, u.tenant_id)
        u.full_name = body.full_name
        if body.role:
            u.role = body.role
        if body.status:
            u.status = body.status
        if body.password:
            u.password = hash_password(body.password)
        s.commit(); s.refresh(u)
        return _user_dict(u, _tenant_name(s, u.tenant_id))


@router.delete("/users/{user_id}")
def users_delete(user_id: int,
                 principal: Principal = Depends(require_role("tenant_admin"))):
    with SessionLocal() as s:
        u = s.get(AppUser, user_id)
        if not u:
            raise HTTPException(404, "user not found")
        assert_tenant_owns(principal, u.tenant_id)
        s.delete(u); s.commit()
        return {"ok": True}


@router.put("/users/{user_id}/profile")
def users_update_profile(user_id: int, body: ProfileBody):
    """Self-service profile update for the signed-in user. Deliberately narrow:
    it updates only the display name — never role/status/email — so a user
    editing their own profile can't change their access (unlike the admin-only
    PUT /users/{user_id})."""
    with SessionLocal() as s:
        u = s.get(AppUser, user_id)
        if not u:
            raise HTTPException(404, "user not found")
        name = (body.full_name or "").strip()
        if not name:
            raise HTTPException(400, "Full name is required.")
        u.full_name = name
        s.commit(); s.refresh(u)
        mga = _tenant_name(s, u.tenant_id)
        _log(mga, None, "profile_updated", target=str(u.id))
        return _user_dict(u, mga)
