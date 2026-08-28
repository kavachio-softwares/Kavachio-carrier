// Redux store with redux-persist: the auth slice is persisted to localStorage
// under "persist:kavachio-auth" and rehydrated on app start (PersistGate in
// main.tsx holds rendering until then, so tokens are always available by the
// time components mount and fire api calls).

import { combineReducers, configureStore } from "@reduxjs/toolkit";
import {
  FLUSH, PAUSE, PERSIST, PURGE, REGISTER, REHYDRATE,
  persistReducer, persistStore,
} from "redux-persist";
import storage from "redux-persist/lib/storage"; // localStorage
import { authReducer } from "./authSlice";

const rootReducer = combineReducers({
  auth: persistReducer(
    {
      key: "kavachio-auth",
      storage,
      version: 1,
      // Never write the short-lived access token to localStorage — it lives in
      // memory only. On reload the first 401 mints a fresh one from the
      // persisted refresh token (client.ts interceptor), so sessions still
      // survive reloads while the bearer token itself is never at rest.
      blacklist: ["accessToken"],
    },
    authReducer,
  ),
});

export const store = configureStore({
  reducer: rootReducer,
  middleware: (getDefaultMiddleware) =>
    getDefaultMiddleware({
      serializableCheck: {
        // redux-persist's control actions carry non-serializable payloads by design.
        ignoredActions: [FLUSH, REHYDRATE, PAUSE, PERSIST, PURGE, REGISTER],
      },
    }),
});

export const persistor = persistStore(store);

// Once the persisted store is bootstrapped it owns the auth state — drop the
// legacy hand-rolled keys so there's exactly one source of truth. (The auth
// slice read them as its initial state, so nothing is lost.)
const LEGACY_KEYS = ["kavachio_user", "kavachio_access", "kavachio_refresh", "kavachio_tenant", "mga"];
const unsub = persistor.subscribe(() => {
  if (persistor.getState().bootstrapped) {
    LEGACY_KEYS.forEach((k) => localStorage.removeItem(k));
    unsub();
  }
});

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;
