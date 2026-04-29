import React, { createContext, useContext, useState, useEffect, useCallback } from "react";
import api from "../lib/api";

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

  useEffect(() => {
    refresh().finally(() => setLoading(false));
  }, [refresh]);

  const login = async (email, password) => {
    await api.post("/auth/login", { email, password });
    // Verify the cookie was accepted by the browser before we let the app
    // render protected routes. Without this, a silent Set-Cookie failure
    // (Secure/SameSite/cross-domain quirks) presents later as "Not authenticated".
    const me = await api.get("/auth/me");
    setUser(me.data);
    return me.data;
  };

  const register = async (name, email, password) => {
    await api.post("/auth/register", { name, email, password });
    const me = await api.get("/auth/me");
    setUser(me.data);
    return me.data;
  };

  const logout = async () => {
    try { await api.post("/auth/logout"); } catch (_) {}
    setUser(false);
  };

  return (
    <AuthContext.Provider value={{ user, loading, login, register, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => useContext(AuthContext);
