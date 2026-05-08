import axios from "axios";
import { getCurrentIdToken, FIREBASE_ENABLED } from "./firebase";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const TOKEN_KEY = "soulprint_token";
const LOCATION_KEY = "soulprint_location";  // "City, Country" cached after one geocode

export const getToken = () => {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
};
export const setToken = (token) => {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {/* ignore quota / private mode */}
};

export const getCachedLocation = () => {
  try { return localStorage.getItem(LOCATION_KEY) || ""; } catch { return ""; }
};
export const setCachedLocation = (loc) => {
  try {
    if (loc) localStorage.setItem(LOCATION_KEY, loc);
    else localStorage.removeItem(LOCATION_KEY);
  } catch {/* ignore */}
};

/** Strip internal [SYSTEM CONTEXT — DO NOT IGNORE] + [USER ATTACHMENTS]
 * coaching blocks that the backend appends to mission goals. Users see
 * only the ask they actually typed — the coaching stays in the LLM's prompt.
 */
export const displayMissionGoal = (goal) => {
  if (!goal) return "";
  const idxSys = goal.indexOf("\n\n[SYSTEM CONTEXT");
  const idxAtt = goal.indexOf("\n\n[USER ATTACHMENTS");
  const idxCont = goal.indexOf("\n\n[Continuation request");
  let cut = goal.length;
  if (idxSys !== -1) cut = Math.min(cut, idxSys);
  if (idxAtt !== -1) cut = Math.min(cut, idxAtt);
  if (idxCont !== -1) cut = Math.min(cut, idxCont);
  return goal.slice(0, cut).trim();
};

/** Resolve (and cache) a friendly "City, Country" string from the browser's
 * geolocation API. Resolves to "" silently if denied/unavailable. */
export const captureUserLocation = () => new Promise((resolve) => {
  if (!navigator?.geolocation) return resolve("");
  const cached = getCachedLocation();
  if (cached) return resolve(cached);
  navigator.geolocation.getCurrentPosition(
    async (pos) => {
      const { latitude: lat, longitude: lon } = pos.coords;
      try {
        const r = await fetch(
          `https://nominatim.openstreetmap.org/reverse?format=json&zoom=10&lat=${lat}&lon=${lon}`,
          { headers: { "Accept-Language": "en" } },
        );
        const d = await r.json();
        const a = d.address || {};
        const city = a.city || a.town || a.village || a.hamlet || a.county || "";
        const region = a.state || a.region || "";
        const country = a.country || "";
        const label = [city, region, country].filter(Boolean).join(", ");
        if (label) setCachedLocation(label);
        resolve(label || `${lat.toFixed(2)},${lon.toFixed(2)}`);
      } catch {
        resolve(`${lat.toFixed(2)},${lon.toFixed(2)}`);
      }
    },
    () => resolve(""),
    { timeout: 8000, maximumAge: 24 * 3600 * 1000 },
  );
});

const userTimezone = () => {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; }
  catch { return "UTC"; }
};

const api = axios.create({
  baseURL: `${BACKEND_URL}/api`,
  // 180-second timeout — covers multi-agent replies + web-search-grounded
  // agents that occasionally cross 100s. Cloudflare itself will sever
  // anything beyond ~100s upstream, but the explicit client-side timeout
  // gives us a clean ECONNABORTED instead of a vague "Network Error",
  // and a faster fail when the backend really did crash mid-request.
  timeout: 180000,
  // No `withCredentials` — we authenticate with a Bearer token from
  // localStorage (set in the request interceptor below). Enabling cookie
  // credentials forces the browser to require *credentialed* CORS headers
  // (`Allow-Origin` echoing the exact origin + `Allow-Credentials: true`),
  // which the preview ingress rewrites to `Allow-Origin: *` on preflights —
  // causing intermittent "Network Error" toasts on the first request after
  // a preflight cache miss. Dropping it makes preflights non-credentialed
  // so the wildcard origin is accepted, and the Bearer header is enough
  // for auth (the backend `get_current_user` accepts either header or cookie).
});

// Always also send the JWT as a Bearer token. The backend accepts either
// the HttpOnly cookie OR the Authorization header — this gives us a robust
// fallback for browsers / extensions that silently drop third-party cookies
// or get into weird Set-Cookie states across HTTP/HTTPS schemes.
//
// We additionally tag every request with the user's timezone (always
// available via Intl) and — if previously granted — their approximate
// location, so agents can quote "today" / "your local time" correctly.
api.interceptors.request.use(async (config) => {
  config.headers = config.headers || {};
  // Prefer a fresh Firebase ID token when the user signed in via Firebase
  // (Google or Firebase email/password). The Firebase SDK auto-refreshes
  // tokens once they get within 5 min of expiry, so getIdToken() returns
  // a valid token without any network call most of the time.
  let bearer = "";
  if (FIREBASE_ENABLED) {
    try { bearer = await getCurrentIdToken(); } catch { bearer = ""; }
  }
  // Fallback to the legacy bcrypt/HS256 token for users who signed in via
  // the existing email+password flow (no Firebase session active).
  if (!bearer) bearer = getToken();
  if (bearer) config.headers.Authorization = `Bearer ${bearer}`;
  config.headers["X-User-Timezone"] = userTimezone();
  const loc = getCachedLocation();
  if (loc) config.headers["X-User-Location"] = loc;
  return config;
});

// Response interceptor — flatten FastAPI/Pydantic validation errors into a
// plain string on `err.response.data.detail` before user code ever sees it.
// Otherwise `toast.error(err.response.data.detail)` would pass an array of
// objects to sonner, which tries to render it as a React child and throws
// "Objects are not valid as a React child" — killing the whole page.
api.interceptors.response.use(
  (resp) => resp,
  (err) => {
    const d = err?.response?.data?.detail;
    if (Array.isArray(d) || (d && typeof d === "object" && !Array.isArray(d))) {
      err.response.data.detail = errorMessage(err, "Request failed");
    }
    return Promise.reject(err);
  },
);

export default api;
export { BACKEND_URL };

/**
 * errorMessage — safely extract a user-facing string from an axios error.
 *
 * Axios surfaces FastAPI validation errors as
 * ``{ detail: [ {type, loc, msg, input, url}, ... ] }``. Calling
 * ``toast.error(err.response.data.detail)`` with that payload passes an
 * array-of-objects to sonner, which tries to render it as a React child
 * and throws "Objects are not valid as a React child" — killing the whole
 * page. Use this helper instead for any error toast / banner.
 */
export function errorMessage(err, fallback = "Something went wrong") {
  const d = err?.response?.data?.detail;
  if (typeof d === "string" && d.trim()) return d;
  if (Array.isArray(d) && d.length > 0) {
    // Stringify each Pydantic error object — keep the first two to stay short.
    return d.slice(0, 2)
      .map((e) => {
        if (!e || typeof e !== "object") return String(e);
        const loc = Array.isArray(e.loc) ? e.loc.slice(1).join(".") : "";
        const m = e.msg || e.message || "";
        return loc ? `${loc}: ${m}` : m;
      })
      .filter(Boolean)
      .join(" · ") || fallback;
  }
  if (d && typeof d === "object") return d.msg || d.message || fallback;
  return err?.message || fallback;
}

/**
 * downloadFile — bulletproof download for capability-URL artifacts.
 *
 * `<a href="…" download>` is unreliable cross-origin: Chrome / Safari often
 * navigate to the URL and ignore the `download` attribute when the link
 * points to a different origin (which our `/api/files/docs/...` always
 * does in production). Even our backend `Content-Disposition: attachment`
 * header doesn't always win — popup blockers, mobile Safari, and PWA
 * installs sometimes still render markdown / JSON inline.
 *
 * This helper fetches the bytes via fetch() (same-origin Bearer if needed)
 * and triggers a fully-programmatic download via a Blob URL, which every
 * browser honours.
 */
export async function downloadFile(url, suggestedName = "download") {
  const absolute = url.startsWith("http") ? url : `${BACKEND_URL}${url}`;
  const t = getToken();
  const headers = {};
  // Capability URLs (random uuid filenames) don't require auth, but adding
  // the Bearer never hurts and keeps the helper usable for protected docs.
  if (t) headers.Authorization = `Bearer ${t}`;
  const res = await fetch(absolute, { headers, credentials: "include" });
  if (!res.ok) throw new Error(`Download failed (${res.status})`);
  const blob = await res.blob();
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = suggestedName;
  // Anchor must be in DOM for Firefox to honour the click programmatically.
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke after a tick so Safari finishes the download first.
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}
