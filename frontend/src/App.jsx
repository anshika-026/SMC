import { lazy, Suspense } from "react";
import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import { AuthProvider } from "./context/AuthContext";
import ProtectedRoute from "./components/ProtectedRoute";
import AdminRoute from "./components/AdminRoute";
import ErrorBoundary from "./components/ErrorBoundary";
import AppShell from "./layouts/AppShell";
import { featureOn, licenseOn } from "./lib/features";

import Login from "./pages/Login";
import ClientLogin from "./pages/ClientLogin";
import Sso from "./pages/Sso";

// Pages load on demand: the app used to ship every page (and the charting
// library) in one ~850 kB bundle before the login screen could render.
const Dashboard = lazy(() => import("./pages/Dashboard"));
const LiveFeed = lazy(() => import("./pages/LiveFeed"));
const PlainLiveFeed = lazy(() => import("./pages/PlainLiveFeed"));
const Alerts = lazy(() => import("./pages/Alerts"));
const People = lazy(() => import("./pages/People"));
const Attendance = lazy(() => import("./pages/Attendance"));
const FaceRecognition = lazy(() => import("./pages/FaceRecognition"));
const Workforce = lazy(() => import("./pages/Workforce"));
const VisitorManagement = lazy(() => import("./pages/VisitorManagement"));
const StaffCount = lazy(() => import("./pages/StaffCount"));
const FootfallUat = lazy(() => import("./pages/FootfallUat"));
const CameraManagement = lazy(() => import("./pages/CameraManagement"));
const SiteManagement = lazy(() => import("./pages/SiteManagement"));
const LicenseManagement = lazy(() => import("./pages/LicenseManagement"));
const FaceTraining = lazy(() => import("./pages/FaceTraining"));

const SettingsLayout = lazy(() => import("./pages/settings/SettingsLayout"));
const Profile = lazy(() => import("./pages/settings/Profile"));
const Notifications = lazy(() => import("./pages/settings/Notifications"));
const RulesPolicy = lazy(() => import("./pages/settings/RulesPolicy"));
const HolidayCalendar = lazy(() => import("./pages/settings/HolidayCalendar"));

function PageFallback() {
  return <div className="p-8 text-sm text-slate-500">Loading…</div>;
}

// Error boundary + lazy-load boundary per route (keyed by path, so moving to
// another page recovers from a crashed one).
function Page({ children }) {
  const { pathname } = useLocation();
  return (
    <ErrorBoundary key={pathname}>
      <Suspense fallback={<PageFallback />}>{children}</Suspense>
    </ErrorBoundary>
  );
}

// A page for an AI feature this deployment doesn't offer, or that the Master
// Portal license doesn't include (`license`, lib/features.js), redirects to
// the dashboard instead of rendering. The backend refuses its API either way.
function Feature({ name, license, children }) {
  return featureOn(name) && licenseOn(license) ? children : <Navigate to="/dashboard" replace />;
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/client-login" element={<ClientLogin />} />
          {/* Dev-mode equivalent of client-<slug>.decovision.com — see
              backend/app/license_routes.py's public_company_by_slug and
              the report's production-deployment notes for real subdomains. */}
          <Route path="/client/:slug/login" element={<ClientLogin />} />
          {/* Arrival from the Deco Vision Master Portal sign-in (launch token in the #fragment). */}
          <Route path="/sso" element={<Sso />} />
          {/* No self-signup: admin accounts are created by an operator
              (python -m app.manage create-admin), clients by an admin. */}
          <Route
            path="/face-training"
            element={
              <Feature name="face_recognition" license="face_training">
                <AdminRoute>
                  <Page>
                    <FaceTraining />
                  </Page>
                </AdminRoute>
              </Feature>
            }
          />

          <Route
            element={
              <ProtectedRoute>
                <AppShell />
              </ProtectedRoute>
            }
          >
            <Route path="/dashboard" element={<Page><Dashboard /></Page>} />
            <Route path="/live-feed" element={<Page><PlainLiveFeed /></Page>} />
            <Route path="/live-camera" element={<Feature name="live_overlay"><Page><LiveFeed /></Page></Feature>} />
            <Route path="/alerts" element={<Page><Alerts /></Page>} />
            <Route path="/people" element={<Feature name="face_recognition"><Page><People /></Page></Feature>} />
            <Route path="/face-recognition" element={<Feature name="face_recognition"><Page><FaceRecognition /></Page></Feature>} />
            <Route path="/attendance" element={<Feature name="face_recognition" license="attendance"><Page><Attendance /></Page></Feature>} />
            <Route path="/staff" element={<Feature name="staff_count"><Page><StaffCount /></Page></Feature>} />
            <Route path="/workforce" element={<Feature name="desk_analytics"><Page><Workforce /></Page></Feature>} />
            <Route path="/visitors" element={<Feature name="visitors"><Page><VisitorManagement /></Page></Feature>} />
            <Route path="/footfall" element={<Navigate to="/visitors?tab=footfall" replace />} />
            <Route path="/footfall-uat" element={<Feature name="footfall"><AdminRoute><Page><FootfallUat /></Page></AdminRoute></Feature>} />
            <Route path="/cameras" element={<AdminRoute><Page><CameraManagement /></Page></AdminRoute>} />
            <Route path="/sites" element={<AdminRoute><Page><SiteManagement /></Page></AdminRoute>} />
            <Route path="/licenses" element={<AdminRoute><Page><LicenseManagement /></Page></AdminRoute>} />

            <Route path="/settings" element={<Page><SettingsLayout /></Page>}>
              <Route index element={<Navigate to="profile" replace />} />
              <Route path="profile" element={<Page><Profile /></Page>} />
              <Route path="notifications" element={<Page><Notifications /></Page>} />
              <Route path="rules-policy" element={<Page><RulesPolicy /></Page>} />
              <Route path="holiday-calendar" element={<Page><HolidayCalendar /></Page>} />
            </Route>
          </Route>

          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}
