import React, { createContext, useContext, useState, useEffect } from 'react';
import axios from 'axios';

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;

const AuthContext = createContext(null);

export const AuthProvider = ({ children }) => {
  // null = checking, false = not logged in, object = logged in user
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    axios
      .get(`${BACKEND_URL}/api/auth/me`, { withCredentials: true })
      .then(res => setUser(res.data))
      .catch(() => setUser(false))
      .finally(() => setLoading(false));
  }, []);

  const login = async (email, password) => {
    const { data } = await axios.post(
      `${BACKEND_URL}/api/auth/login`,
      { email, password },
      { withCredentials: true }
    );
    setUser(data);
    return data;
  };

  const register = async (name, email, password) => {
    const { data } = await axios.post(
      `${BACKEND_URL}/api/auth/register`,
      { name, email, password },
      { withCredentials: true }
    );
    setUser(data);
    return data;
  };

  const logout = async () => {
    try {
      await axios.post(`${BACKEND_URL}/api/auth/logout`, {}, { withCredentials: true });
    } catch (_) {}
    setUser(false);
  };

  return (
    <AuthContext.Provider value={{ user, loading, login, register, logout }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => useContext(AuthContext);
