import "./App.css";
import { useEffect } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "./context/AuthContext";
import { ThemeProvider } from "./context/ThemeContext";
import { Toaster } from "./components/ui/sonner";
import { installClipboardSanitiser } from "./lib/clipboard";

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
import AuditsPage from "./pages/AuditsPage";
import AboutPage from "./pages/AboutPage";
import SecurityPage from "./pages/SecurityPage";
import VerifyPublicPage from "./pages/VerifyPublicPage";
import TrustPage from "./pages/TrustPage";
import TrialRedeemPage from "./pages/TrialRedeemPage";
import GetAuditPage from "./pages/GetAuditPage";
import PricingPage from "./pages/PricingPage";
import PrivacyPage from "./pages/PrivacyPage";
import TermsPage from "./pages/TermsPage";
import SubProcessorsPage from "./pages/SubProcessorsPage";
import SOC2RoadmapPage from "./pages/SOC2RoadmapPage";
import UseCasesPage from "./pages/UseCasesPage";
import DocsSocialQueuePage from "./pages/DocsSocialQueuePage";
import MockupHomePage from "./pages/MockupHomePage";
import MockupPricingPage from "./pages/MockupPricingPage";
import MockupHomeV2Page from "./pages/MockupHomeV2Page";
import MockupPricingV2Page from "./pages/MockupPricingV2Page";
import GitHubCallbackPage from "./pages/GitHubCallbackPage";
import AccountPage from "./pages/AccountPage";
import BillingPage from "./pages/BillingPage";
import DeckPage from "./pages/DeckPage";
import AdminDashboard from "./pages/AdminDashboard";
import PublicAuditPage from "./pages/PublicAuditPage";
import ErrorBoundary from "./components/ErrorBoundary";
import SiteAccessGate from "./components/SiteAccessGate";

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
  if (!user) return <Navigate to="/auth" replace />;
  return <SiteAccessGate>{children}</SiteAccessGate>;
};

const PublicOnly = ({ children }) => {
  const { user, loading } = useAuth();
  if (loading) return children;
  return user ? <Navigate to="/app" replace /> : children;
};

function App() {
  // Strip dark-theme colours / backgrounds from anything the user copies
  // so pasting into Notion / Gmail / Docs renders as black-on-white
  // instead of unreadable white-on-grey. Idempotent install.
  useEffect(() => { installClipboardSanitiser(); }, []);
  return (
    <ThemeProvider>
      <div className="App sp-grain min-h-screen">
        <AuthProvider>
          <BrowserRouter>
            <ErrorBoundary>
              <Routes>
                <Route path="/" element={<PublicOnly><LandingPage /></PublicOnly>} />
                <Route path="/about" element={<AboutPage />} />
                <Route path="/security" element={<SecurityPage />} />
                <Route path="/v/:fileId" element={<VerifyPublicPage />} />
                <Route path="/trust" element={<TrustPage />} />
                <Route path="/trial/redeem" element={<TrialRedeemPage />} />
                <Route path="/get-audit" element={<GetAuditPage />} />
                <Route path="/pricing" element={<PricingPage />} />
                <Route path="/legal/privacy" element={<PrivacyPage />} />
                <Route path="/legal/terms" element={<TermsPage />} />
                <Route path="/legal/sub-processors" element={<SubProcessorsPage />} />
                <Route path="/legal/soc2-roadmap" element={<SOC2RoadmapPage />} />
                <Route path="/use-cases" element={<UseCasesPage />} />
                <Route path="/docs/social-queue" element={<DocsSocialQueuePage />} />
                <Route path="/mockup/home" element={<MockupHomePage />} />
                <Route path="/mockup/pricing" element={<MockupPricingPage />} />
                <Route path="/mockup/home-v2" element={<MockupHomeV2Page />} />
                <Route path="/mockup/pricing-v2" element={<MockupPricingV2Page />} />
                <Route path="/auth/github/callback" element={<Protected><GitHubCallbackPage /></Protected>} />
                <Route path="/deck" element={<DeckPage />} />
                <Route path="/auth" element={<PublicOnly><AuthPage /></PublicOnly>} />
                <Route path="/invite/:code" element={<Protected><AcceptInvitePage /></Protected>} />
                <Route path="/app" element={<Protected><AppHome /></Protected>} />
                <Route path="/app/account" element={<Protected><AccountPage /></Protected>} />
                <Route path="/app/org/:orgId" element={<Protected><OrgPage /></Protected>} />
                <Route path="/app/billing" element={<Protected><BillingPage /></Protected>} />
                <Route path="/app/ws/:wsId" element={<Protected><WorkspaceSettingsPage /></Protected>} />
                <Route path="/app/org/:orgId/map" element={<Protected><OrgMapPage /></Protected>} />
                <Route path="/app/org/:orgId/social" element={<Protected><SocialPage /></Protected>} />
                <Route path="/app/org/:orgId/missions" element={<Protected><MissionsPage /></Protected>} />
                <Route path="/app/org/:orgId/missions/:missionId" element={<Protected><MissionsPage /></Protected>} />
                <Route path="/app/audits" element={<Protected><AuditsPage /></Protected>} />
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
