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
    try {
      const { data } = await api.get("/auth/me");
      setUser(data);
      return data;
    } catch (_) {
      setUser(false);
      return null;
    }
  }, []);

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
    const me = await api.get("/auth/me");
    setUser(me.data);
    return me.data;
  };

  const loginWithFirebaseEmail = async (email, password) => {
    if (!FIREBASE_ENABLED) throw new Error("Firebase not configured");
    await fbSignInWithEmail(email, password);
    setToken("");
    const me = await api.get("/auth/me");
    setUser(me.data);
    return me.data;
  };

  const registerWithFirebaseEmail = async (name, email, password) => {
    if (!FIREBASE_ENABLED) throw new Error("Firebase not configured");
    await fbRegisterWithEmail({ name, email, password });
    setToken("");
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
