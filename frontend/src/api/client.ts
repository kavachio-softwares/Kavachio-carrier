import axios from "axios";
import { getAccessToken, getRefreshToken, isAccessTokenStale, setAccessToken, clearAuth } from "../auth";

declare module "axios" {
  export interface AxiosRequestConfig {
    /** Skip the global network-activity counter (no app-wide loading overlay)
     *  — for callers that show their own local/scoped loading affordance,
     *  e.g. infinite-scroll pagination fetches. */
    silent?: boolean;
  }
}

const baseURL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export const api = axios.create({ baseURL });

// --- global network activity --------------------------------------------
// Counts in-flight requests so the UI (GlobalNetworkBar in components/Busy)
// can show a top-of-page progress bar whenever ANY api call is running —
// no per-callsite loading state needed for feedback.
let inflightCount = 0;
const netListeners = new Set<() => void>();
function bumpNet(delta: number) {
  inflightCount = Math.max(0, inflightCount + delta);
  netListeners.forEach((fn) => fn());
}
export function netActive(): boolean {
  return inflightCount > 0;
}
export function subscribeNetActivity(fn: () => void): () => void {
  netListeners.add(fn);
  return () => { netListeners.delete(fn); };
}

// --- global friendly error popup ------------------------------------------
// Raw server failures (HTTP 5xx, unreachable server) must never surface in the
// UI as "Internal Server Error" / stack text. The interceptor at the bottom of
// this file rewrites those errors to a friendly message and publishes here so
// <GlobalErrorPopup/> (mounted once in App) can show a popup — no per-page
// wiring needed.
export type ApiErrorNotice = { kind: "server" | "network"; message: string };
const errorListeners = new Set<(n: ApiErrorNotice) => void>();
export function subscribeApiErrors(fn: (n: ApiErrorNotice) => void): () => void {
  errorListeners.add(fn);
  return () => { errorListeners.delete(fn); };
}
function publishApiError(n: ApiErrorNotice) {
  errorListeners.forEach((fn) => fn(n));
}

export const FRIENDLY_SERVER_ERROR =
  "Something went wrong on our side while processing your request. Please try again in a moment.";
export const FRIENDLY_NETWORK_ERROR =
  "We couldn't reach the server. Please check your connection and try again.";

// Attach the access token to every request in one place.
//
// The access token is memory-only (redux-persist blacklist), so after a page
// reload it's gone while the refresh token survives — and during a long
// session it simply expires (1h). In both cases, mint a fresh one BEFORE the
// request goes out (all concurrent callers await the same refresh) instead of
// letting the call 401 and be replayed — the replay is what shows every
// endpoint twice in the network tab.
api.interceptors.request.use(async (cfg) => {
  // `silent: true` (e.g. infinite-scroll pagination fetches) opts a request out
  // of the global counter — it still runs normally, it just never triggers the
  // app-wide <GlobalLoadingOverlay/>, since those callers show their own
  // scoped, local loading affordance instead.
  if (!cfg.silent) bumpNet(+1);
  let t = getAccessToken();
  const isAuthCall = typeof cfg.url === "string" && cfg.url.includes("/auth/");
  // `refreshFailedAt` breaks a refresh storm: refreshAccessToken() resolves to
  // null on failure, so without a cooldown EVERY subsequent request retried it
  // — adding a doomed round-trip to each call for the rest of the session.
  const cooling = Date.now() - refreshFailedAt < REFRESH_COOLDOWN_MS;
  if (isAccessTokenStale() && !isAuthCall && getRefreshToken() && !cooling) {
    refreshing = refreshing ?? refreshAccessToken();
    const fresh = await refreshing;
    refreshing = null;
    if (fresh) { t = fresh; refreshFailedAt = 0; }
    else refreshFailedAt = Date.now();
  }
  if (t) cfg.headers.Authorization = `Bearer ${t}`;
  return cfg;
});

// Decrement the in-flight counter on settle (registered before the other
// response interceptors so it runs for every outcome; a 401 replay re-enters
// the request interceptor and counts as its own request).
api.interceptors.response.use(
  (r) => { if (!r.config.silent) bumpNet(-1); return r; },
  (err) => { if (!err.config?.silent) bumpNet(-1); return Promise.reject(err); }
);

// Long-running endpoints (contract upload) stream heartbeat whitespace and
// always complete as HTTP 200 — a pipeline failure arrives IN the body as
// {success:false, error:true, status_code, detail}. Convert that back into a
// rejected axios error so callers' existing catch/err.response.data.detail
// paths keep working unchanged.
api.interceptors.response.use((r) => {
  const d = r.data as
    | { success?: boolean; error?: boolean; status_code?: number; detail?: unknown }
    | null;
  if (d && d.error === true && d.success === false && d.detail !== undefined) {
    const status = d.status_code ?? 500;
    const err = new axios.AxiosError(
      typeof d.detail === "string" ? d.detail : "Request failed",
      String(status),
      r.config,
      r.request,
      { ...r, status, data: { detail: d.detail } },
    );
    return Promise.reject(err);
  }
  return r;
});

// De-dupe concurrent refreshes: if several requests 401 at once, they all await
// the same in-flight refresh instead of firing N /auth/refresh calls.
let refreshing: Promise<string | null> | null = null;
// When a refresh fails, stop pre-emptively retrying it on every request for a
// short window. Without this a failing refresh token silently prepends a doomed
// /auth/refresh round-trip to every API call for the rest of the session.
// Requests still go out (and a real 401 still drives logout) — they just stop
// paying for a refresh that is known to be failing.
const REFRESH_COOLDOWN_MS = 30_000;
let refreshFailedAt = 0;

async function refreshAccessToken(): Promise<string | null> {
  const rt = getRefreshToken();
  if (!rt) return null;
  try {
    // Bare axios (not `api`) so this call skips the interceptors below.
    const { data } = await axios.post(`${baseURL}/auth/refresh`, { refresh_token: rt });
    setAccessToken(data.access_token);
    return data.access_token as string;
  } catch {
    return null; // refresh token expired/invalid → caller logs out
  }
}

// De-dupe identical concurrent GETs: Layout (sidebar brand) and pages like
// /tenant both fetch GET /tenants/{mga} at the same moment on mount — share
// one in-flight request instead of firing two. The entry is removed as soon
// as the request settles, so later calls (e.g. reload after a save) always
// hit the network fresh.
const inflightGets = new Map<string, Promise<unknown>>();
export function getDeduped<T = unknown>(
  url: string, params?: Record<string, unknown>,
): Promise<{ data: T }> {
  const key = url + JSON.stringify(params ?? {});
  const hit = inflightGets.get(key);
  if (hit) return hit as Promise<{ data: T }>;
  const p = api.get<T>(url, { params }).finally(() => inflightGets.delete(key));
  inflightGets.set(key, p);
  return p;
}

/** A valid access token, refreshing first if the current one is stale.
 *  Same rule the request interceptor applies, exposed for callers that bypass
 *  axios (streaming reads go through fetch, which axios can't surface). */
export async function ensureAccessToken(): Promise<string | null> {
  let t = getAccessToken();
  if (isAccessTokenStale() && getRefreshToken()) {
    refreshing = refreshing ?? refreshAccessToken();
    t = (await refreshing) ?? t;
    refreshing = null;
  }
  return t;
}

/** Consume a newline-delimited-JSON endpoint, invoking `onMessage` for each
 *  line AS IT ARRIVES rather than after the response completes.
 *
 *  axios buffers a response into memory before resolving, which defeats the
 *  point of a streamed body, so this goes straight to fetch and reads the
 *  ReadableStream. A partial line at the end of a network chunk is held over
 *  and prefixed onto the next one — chunk boundaries are arbitrary and don't
 *  respect line breaks.
 *
 *  Deliberately outside the global network counter: streams are long-lived by
 *  design, and holding the app-wide overlay up for their whole duration would
 *  block the page. Callers show their own progress instead. Pass `signal` to
 *  abort (e.g. when the component unmounts mid-stream).
 */
export async function streamNdjson(
  path: string,
  params: Record<string, string | number | boolean | undefined>,
  onMessage: (msg: any) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = await ensureAccessToken();
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) qs.set(k, String(v));
  }
  // fetch bypasses the axios interceptors, so this path applies the same
  // friendly-error safeguard itself: never let raw 5xx internals or a bare
  // "Failed to fetch" reach the caller's error rendering.
  let res: Response;
  try {
    res = await fetch(`${baseURL}${path}?${qs.toString()}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal,
    });
  } catch (e) {
    if ((e as DOMException)?.name === "AbortError") throw e; // deliberate cancel
    publishApiError({ kind: "network", message: FRIENDLY_NETWORK_ERROR });
    throw new Error(FRIENDLY_NETWORK_ERROR);
  }
  if (!res.ok) {
    let detail: unknown = `Request failed (${res.status})`;
    try { detail = (await res.json())?.detail ?? detail; } catch { /* non-JSON body */ }
    if (res.status >= 500 || typeof detail !== "string") {
      publishApiError({ kind: "server", message: FRIENDLY_SERVER_ERROR });
      throw new Error(FRIENDLY_SERVER_ERROR);
    }
    throw new Error(detail);
  }
  if (!res.body) throw new Error("This browser cannot read streamed responses.");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl: number;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) onMessage(JSON.parse(line));
    }
  }
  const tail = buf.trim();
  if (tail) onMessage(JSON.parse(tail));
}

// Authenticated file download. Plain <a href> navigations don't carry the
// Authorization header, so file endpoints (now token-guarded) would 401 —
// instead we fetch the bytes through the interceptor and hand the browser a
// blob URL to save.
export async function downloadFile(path: string, filename?: string): Promise<void> {
  const res = await api.get(path, { responseType: "blob" });
  // Prefer the server's filename (Content-Disposition) over the caller's hint.
  const cd = (res.headers?.["content-disposition"] as string | undefined) ?? "";
  const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const name = (m ? decodeURIComponent(m[1]) : filename) ?? path.split("/").pop() ?? "download";
  const url = URL.createObjectURL(res.data);
  const a = document.createElement("a");
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30_000);
}

// On 401, try a refresh ONCE and replay the request. If the refresh token is
// expired/invalid, hard-logout and send the user to /login.
api.interceptors.response.use(
  (r) => r,
  async (err) => {
    const cfg = err.config;
    const status = err.response?.status;
    const isAuthCall = typeof cfg?.url === "string" && cfg.url.includes("/auth/");

    if (status === 401 && cfg && !cfg._retry && !isAuthCall) {
      cfg._retry = true;
      refreshing = refreshing ?? refreshAccessToken();
      const newToken = await refreshing;
      refreshing = null;

      if (newToken) {
        cfg.headers = cfg.headers ?? {};
        cfg.headers.Authorization = `Bearer ${newToken}`;
        return api(cfg); // replay the original request
      }

      clearAuth();
      if (window.location.pathname !== "/login") window.location.assign("/login");
    }
    return Promise.reject(err);
  }
);

// Last line of defence — registered AFTER the 401 handler so it only sees
// errors that survive the refresh/replay flow. Unexpected failures (HTTP 5xx,
// server unreachable) carry raw internals in `detail` (str(e), tracebacks,
// bare "Internal Server Error"); rewrite them to a friendly message so every
// existing `e?.response?.data?.detail` callsite renders friendly text, and
// publish so <GlobalErrorPopup/> shows a popup. Meaningful 4xx messages
// (validation, not-found, conflicts) pass through untouched — pages already
// present those well.
api.interceptors.response.use(
  (r) => r,
  (err) => {
    // Deliberate cancellations (unmounts, aborted uploads) aren't failures.
    if (axios.isCancel(err) || err.code === "ERR_CANCELED") return Promise.reject(err);
    // A 401 that got here is being handled by the session flow above.
    const status = err.response?.status;
    if (status === 401) return Promise.reject(err);
    // A replayed request already ran through this interceptor once (the inner
    // api(cfg) call has its own full chain) — don't popup twice for one failure.
    if (err._friendlyHandled) return Promise.reject(err);

    if (!err.response) {
      // No response at all: server down, DNS/connection failure, CORS block.
      err._friendlyHandled = true;
      err.message = FRIENDLY_NETWORK_ERROR;
      publishApiError({ kind: "network", message: FRIENDLY_NETWORK_ERROR });
    } else if (typeof status === "number" && status >= 500) {
      err._friendlyHandled = true;
      const data = err.response.data;
      err.response.data = {
        ...(data && typeof data === "object" && !(data instanceof Blob) ? data : {}),
        detail: FRIENDLY_SERVER_ERROR,
      };
      err.message = FRIENDLY_SERVER_ERROR;
      publishApiError({ kind: "server", message: FRIENDLY_SERVER_ERROR });
    }
    return Promise.reject(err);
  }
);
