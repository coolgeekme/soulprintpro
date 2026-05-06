import "./App.css";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "./context/AuthContext";
import { ThemeProvider } from "./context/ThemeContext";
import { Toaster } from "./components/ui/sonner";

import LandingPage from "./pages/LandingPage";
import AuthPage from "./pages/AuthPage";
import AppHome from "./pages/AppHome";
import OrgPage from "./pages/OrgPage";
import OrgMapPage from "./pages/OrgMapPage";
import TeamPage from "./pages/TeamPage";
import ConversationPage from "./pages/ConversationPage";
import AcceptInvitePage from "./pages/AcceptInvitePage";
import WorkspaceSettingsPage from "./pages/WorkspaceSettingsPage";
import SocialPage from "./pages/SocialPage";
import MissionsPage from "./pages/MissionsPage";
import AboutPage from "./pages/AboutPage";
import AccountPage from "./pages/AccountPage";
import AdminDashboard from "./pages/AdminDashboard";
import PublicAuditPage from "./pages/PublicAuditPage";
import ErrorBoundary from "./components/ErrorBoundary";

const Loader = () => (
  <div className="flex items-center justify-center h-screen">
    <div className="flex flex-col items-center gap-3">
      <div className="w-8 h-8 border-2 border-white/10 border-t-white/70 rounded-full animate-spin" />
      <span className="text-xs tracking-widest uppercase sp-subtle">Loading</span>
    </div>
  </div>
);

const Protected = ({ children }) => {
  const { user, loading } = useAuth();
  if (loading) return <Loader />;
  return user ? children : <Navigate to="/auth" replace />;
};

const PublicOnly = ({ children }) => {
  const { user, loading } = useAuth();
  if (loading) return children;
  return user ? <Navigate to="/app" replace /> : children;
};

function App() {
  return (
    <ThemeProvider>
      <div className="App sp-grain min-h-screen">
        <AuthProvider>
          <BrowserRouter>
            <ErrorBoundary>
              <Routes>
                <Route path="/" element={<PublicOnly><LandingPage /></PublicOnly>} />
                <Route path="/about" element={<AboutPage />} />
                <Route path="/auth" element={<PublicOnly><AuthPage /></PublicOnly>} />
                <Route path="/invite/:code" element={<Protected><AcceptInvitePage /></Protected>} />
                <Route path="/app" element={<Protected><AppHome /></Protected>} />
                <Route path="/app/account" element={<Protected><AccountPage /></Protected>} />
                <Route path="/app/org/:orgId" element={<Protected><OrgPage /></Protected>} />
                <Route path="/app/ws/:wsId" element={<Protected><WorkspaceSettingsPage /></Protected>} />
                <Route path="/app/org/:orgId/map" element={<Protected><OrgMapPage /></Protected>} />
                <Route path="/app/org/:orgId/social" element={<Protected><SocialPage /></Protected>} />
                <Route path="/app/org/:orgId/missions" element={<Protected><MissionsPage /></Protected>} />
                <Route path="/app/org/:orgId/missions/:missionId" element={<Protected><MissionsPage /></Protected>} />
                <Route path="/app/team/:teamId" element={<Protected><TeamPage /></Protected>} />
                <Route path="/app/team/:teamId/c/:convId" element={<Protected><ConversationPage /></Protected>} />
                <Route path="/admin" element={<Protected><AdminDashboard /></Protected>} />
                {/* Public-facing prospect-grade audit landing page (NO AUTH).
                    Lives outside the /app shell so anyone with the link can
                    view the teaser; the lead form gates the full report. */}
                <Route path="/audit/:slug" element={<PublicAuditPage />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </ErrorBoundary>
          </BrowserRouter>
          <Toaster theme="dark" />
        </AuthProvider>
      </div>
    </ThemeProvider>
  );
}

export default App;
