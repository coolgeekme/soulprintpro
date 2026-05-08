import React, { createContext, useContext, useState, useEffect, useCallback } from "react";
import api, { setToken } from "../lib/api";
import {
  FIREBASE_ENABLED,
  signInWithGoogle as fbSignInWithGoogle,
  signInWithEmail as fbSignInWithEmail,
  registerWithEmail as fbRegisterWithEmail,
  resetPassword as fbResetPassword,
  firebaseSignOut,
  onAuthChange,
} from "../lib/firebase";

const AuthContext = createContext(null);

export const AuthProvider = ({ children }) => {
  // null = still checking; false = anonymous; object = signed in
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    // Up to 3 attempts on transient failures (network/502/503/504/timeout)
    // — covers the 5-10s window during a backend redeploy where the
    // load balancer returns 5xx before the new pod is healthy.
    // Importantly we DO NOT retry on 401/403 because that's the signal
    // that the JWT is genuinely invalid.
    const maxAttempts = 3;
    let lastErr;
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      try {
        const { data } = await api.get("/auth/me");
        setUser(data);
        return data;
      } catch (err) {
        lastErr = err;
        const status = err?.response?.status;
        if (status === 401 || status === 403) {
          // Definitive logout — token is invalid.
          setUser(false);
          return null;
        }
        // Transient error → wait and retry. 800ms / 1.6s / 3.2s backoff.
        if (attempt < maxAttempts) {
          await new Promise((r) => setTimeout(r, 800 * 2 ** (attempt - 1)));
        }
      }
    }
    // All retries exhausted. Keep the existing user (so refreshes
    // during the deploy window don't bounce a signed-in user to /auth)
    // and let the next interval-driven refresh recover the session.
    // Note: on initial load with no prior user, this leaves `user`
    // at the previous value (null). The Protected route still
    // redirects in that case — which is the safe fallback when we
    // genuinely can't reach the backend at all.
    if (lastErr && process.env.NODE_ENV !== "production") {
      // eslint-disable-next-line no-console
      console.warn("[auth] /auth/me failed after retries:", lastErr?.message);
    }
    return null;
  }, []);

  // Periodic background re-validation. Without this, the token may
  // silently expire (30 days) while the user is sitting on the
  // dashboard. We probe /auth/me every 5 minutes — cheap, and any 401
  // here will properly sign them out via the branch above.
  useEffect(() => {
    const id = setInterval(() => { refresh(); }, 5 * 60 * 1000);
    return () => clearInterval(id);
  }, [refresh]);

  // Re-pull /auth/me whenever the Firebase ID token changes (sign-in,
  // sign-out, refresh). The axios interceptor already picks the freshest
  // token, so /auth/me is hitting the right user without us doing
  // anything extra here.
  useEffect(() => {
    let initialDone = false;
    const finishInitial = () => {
      if (!initialDone) {
        initialDone = true;
        setLoading(false);
      }
    };
    if (!FIREBASE_ENABLED) {
      // Legacy-only mode — single /auth/me probe and we're done.
      refresh().finally(finishInitial);
      return undefined;
    }
    const unsubscribe = onAuthChange(async (fbUser) => {
      // Re-fetch the Mongo-backed user. If a Firebase session exists
      // /auth/me will return the Firebase-backed user; if not it falls
      // back to whatever legacy JWT is in localStorage; if neither, 401
      // → user becomes false (anonymous).
      await refresh();
      finishInitial();
      // Note: we deliberately don't gate on `fbUser` because a legacy
      // JWT user (no Firebase session) should still keep working.
    });
    return () => { try { unsubscribe(); } catch (_) {} };
  }, [refresh]);

  // ── Legacy email+password (Mongo bcrypt path) ─────────────────────
  const login = async (email, password) => {
    const { data } = await api.post("/auth/login", { email, password });
    if (data?.access_token) setToken(data.access_token);
    const me = await api.get("/auth/me");
    setUser(me.data);
    return me.data;
  };

  const register = async (name, email, password) => {
    const { data } = await api.post("/auth/register", { name, email, password });
    if (data?.access_token) setToken(data.access_token);
    const me = await api.get("/auth/me");
    setUser(me.data);
    return me.data;
  };

  // ── Firebase sign-in flows ────────────────────────────────────────
  const loginWithGoogle = async () => {
    if (!FIREBASE_ENABLED) throw new Error("Firebase not configured");
    await fbSignInWithGoogle();
    // Clear any legacy token so future requests use the Firebase one.
    setToken("");
    // Force a fresh ID token + a small settle delay so the Firebase
    // SDK has fully committed `currentUser` before our axios interceptor
    // pulls the bearer for the upcoming /auth/me call.
    await new Promise((r) => setTimeout(r, 150));
    const me = await api.get("/auth/me");
    setUser(me.data);
    return me.data;
  };

  const loginWithFirebaseEmail = async (email, password) => {
    if (!FIREBASE_ENABLED) throw new Error("Firebase not configured");
    await fbSignInWithEmail(email, password);
    setToken("");
    await new Promise((r) => setTimeout(r, 150));
    const me = await api.get("/auth/me");
    setUser(me.data);
    return me.data;
  };

  const registerWithFirebaseEmail = async (name, email, password) => {
    if (!FIREBASE_ENABLED) throw new Error("Firebase not configured");
    await fbRegisterWithEmail({ name, email, password });
    setToken("");
    await new Promise((r) => setTimeout(r, 150));
    const me = await api.get("/auth/me");
    setUser(me.data);
    return me.data;
  };

  const sendPasswordReset = async (email) => {
    if (!FIREBASE_ENABLED) throw new Error("Firebase not configured");
    await fbResetPassword(email);
  };

  const logout = async () => {
    try { await api.post("/auth/logout"); } catch (_) {}
    setToken("");
    if (FIREBASE_ENABLED) await firebaseSignOut();
    setUser(false);
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        login,
        register,
        logout,
        refresh,
        // Firebase additions
        firebaseEnabled: FIREBASE_ENABLED,
        loginWithGoogle,
        loginWithFirebaseEmail,
        registerWithFirebaseEmail,
        sendPasswordReset,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => useContext(AuthContext);
