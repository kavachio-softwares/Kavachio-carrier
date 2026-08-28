// Auth state slice — the single source of truth for the signed-in user, both
// JWT tokens and the tenant branding. Persisted to localStorage by
// redux-persist (see store/index.ts). Components and non-React code (axios
// interceptors) access it through the helper API in src/auth.ts, which
// delegates here — so call sites didn't change in the Redux migration.

import { createSlice, type PayloadAction } from "@reduxjs/toolkit";

export type User = {
  id: number;
  email: string;
  full_name: string;
  // Raw value as sent by the backend; use userRole()/predicates for logic.
  role: string;
  mga: string;
  tenant_id?: number | null;
};

export type TenantBrand = { mga: string; legal_name?: string | null; logo?: string | null };

export type AuthState = {
  user: User | null;
  accessToken: string | null;
  refreshToken: string | null;
  tenantBrand: TenantBrand | null;
  mga: string | null;
};

// One-time migration from the legacy hand-rolled localStorage keys. On the
// first run after this change there is no redux-persist state yet, so the
// slice boots from the old keys and the next persist write adopts them —
// nobody gets logged out by the migration. (store/index.ts removes the old
// keys once the persisted store is bootstrapped.)
function legacyBootstrapState(): AuthState {
  const empty: AuthState = {
    user: null, accessToken: null, refreshToken: null, tenantBrand: null, mga: null,
  };
  try {
    const user = localStorage.getItem("kavachio_user");
    const brand = localStorage.getItem("kavachio_tenant");
    return {
      user: user ? (JSON.parse(user) as User) : null,
      accessToken: localStorage.getItem("kavachio_access"),
      refreshToken: localStorage.getItem("kavachio_refresh"),
      tenantBrand: brand ? (JSON.parse(brand) as TenantBrand) : null,
      mga: localStorage.getItem("mga"),
    };
  } catch {
    return empty;
  }
}

const authSlice = createSlice({
  name: "auth",
  initialState: legacyBootstrapState,
  reducers: {
    /** Full login response: user profile + both tokens. */
    authSet(state, action: PayloadAction<{ user: User; accessToken?: string; refreshToken?: string }>) {
      state.user = action.payload.user;
      state.mga = action.payload.user.mga;
      if (action.payload.accessToken) state.accessToken = action.payload.accessToken;
      if (action.payload.refreshToken) state.refreshToken = action.payload.refreshToken;
    },
    userSet(state, action: PayloadAction<User>) {
      state.user = action.payload;
      state.mga = action.payload.mga;
    },
    accessTokenSet(state, action: PayloadAction<string>) {
      state.accessToken = action.payload;
    },
    tenantBrandSet(state, action: PayloadAction<TenantBrand>) {
      state.tenantBrand = action.payload;
    },
    /** Logout / expired session: drop everything. */
    authCleared() {
      return {
        user: null, accessToken: null, refreshToken: null, tenantBrand: null, mga: null,
      } as AuthState;
    },
  },
});

export const { authSet, userSet, accessTokenSet, tenantBrandSet, authCleared } = authSlice.actions;
export const authReducer = authSlice.reducer;
