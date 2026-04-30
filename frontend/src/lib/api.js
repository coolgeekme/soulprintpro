import axios from "axios";

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
  withCredentials: true,
});

// Always also send the JWT as a Bearer token. The backend accepts either
// the HttpOnly cookie OR the Authorization header — this gives us a robust
// fallback for browsers / extensions that silently drop third-party cookies
// or get into weird Set-Cookie states across HTTP/HTTPS schemes.
//
// We additionally tag every request with the user's timezone (always
// available via Intl) and — if previously granted — their approximate
// location, so agents can quote "today" / "your local time" correctly.
api.interceptors.request.use((config) => {
  const t = getToken();
  config.headers = config.headers || {};
  if (t) config.headers.Authorization = `Bearer ${t}`;
  config.headers["X-User-Timezone"] = userTimezone();
  const loc = getCachedLocation();
  if (loc) config.headers["X-User-Location"] = loc;
  return config;
});

export default api;
export { BACKEND_URL };
