import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const TOKEN_KEY = "soulprint_token";

export const getToken = () => {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
};
export const setToken = (token) => {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {/* ignore quota / private mode */}
};

const api = axios.create({
  baseURL: `${BACKEND_URL}/api`,
  withCredentials: true,
});

// Always also send the JWT as a Bearer token. The backend accepts either
// the HttpOnly cookie OR the Authorization header — this gives us a robust
// fallback for browsers / extensions that silently drop third-party cookies
// or get into weird Set-Cookie states across HTTP/HTTPS schemes.
api.interceptors.request.use((config) => {
  const t = getToken();
  if (t) {
    config.headers = config.headers || {};
    config.headers.Authorization = `Bearer ${t}`;
  }
  return config;
});

export default api;
export { BACKEND_URL };
