import "./App.css";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import LandingPage from "./pages/LandingPage";
import Dashboard from "./pages/Dashboard";
import { AuthProvider, useAuth } from "./context/AuthContext";

// Protected route: redirects to / if not authenticated
const ProtectedRoute = ({ children }) => {
  const { user, loading } = useAuth();
  if (loading) {
    return (
      <div className="flex items-center justify-center h-screen bg-white">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-gray-200 border-t-gray-900 rounded-full animate-spin" />
          <span className="text-sm text-gray-400">Loading...</span>
        </div>
      </div>
    );
  }
  return user ? children : <Navigate to="/" replace />;
};

// Public route: show page while loading, redirect to /dashboard if already authenticated
const PublicRoute = ({ children }) => {
  const { user, loading } = useAuth();
  if (loading) return children; // show landing page while auth is checked
  return user ? <Navigate to="/dashboard" replace /> : children;
};

function App() {
  return (
    <div className="App">
      <AuthProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<PublicRoute><LandingPage /></PublicRoute>} />
            <Route path="/dashboard" element={<ProtectedRoute><Dashboard /></ProtectedRoute>} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </BrowserRouter>
      </AuthProvider>
    </div>
  );
}

export default App;
