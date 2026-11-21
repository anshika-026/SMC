import { createContext, useContext, useState, useCallback, useEffect, useRef } from "react";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";
import { setLicensedFeatures } from "../lib/features";

const AuthContext = createContext(null);

// The Master Portal license (backend/app/master_link.py summary) decides which
// pages show; "standalone" (or no info yet) hides nothing.
function applyLicense(license) {
  setLicensedFeatures(license?.mode === "master" ? license.features || [] : null);
}

// How often the signed-in operator's license is re-read (/api/auth/me), so a
// feature the Master Portal turns off or on reaches an open tab.
const LICENSE_REFRESH_MS = 60000;

// How often an already-logged-in client's session is re-checked against
// the live license (backend/app/license_routes.py's /client-me, which
// itself re-validates status/expiry on every call — see auth.py's
// load_active_client_license). This is what makes a suspend/feature/
// camera change an admin makes reach an already-open client tab without
// them needing to log out and back in, and is also how a suspended/
// expired license's access actually stops within a bounded time even if
// the client isn't clicking around triggering other API calls.
const CLIENT_SESSION_REFRESH_MS = 60000;

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => {
    const saved = localStorage.getItem("deco_user");
    const parsed = saved ? JSON.parse(saved) : null;
    applyLicense(parsed?.license);   // before the first render, so a refresh never flashes hidden pages
    return parsed;
  });
  const userRef = useRef(user);
  userRef.current = user;

  const login = useCallback(async (email, password, portalTicket = null) => {
    const res = await api.login(email, password, portalTicket);
    localStorage.setItem("deco_token", res.token);
    localStorage.setItem("deco_user", JSON.stringify(res.user));
    setUser(res.user);
    return res.user;
  }, []);

  function clientUserFrom(res) {
    return {
      role: "client",
      name: res.company_name || res.username,
      email: "",
      username: res.username,
      licenseId: res.license_id,
      companyId: res.company_id,
      companyName: res.company_name,
      companyPortalSlug: res.company_portal_slug,
      allowedCameras: res.cameras,
      allowedCameraIds: res.cameras.map((c) => c.id),
      allowedFeatures: res.features,
    };
  }

  // Client-portal login (license username/password, see api.clientLogin) —
  // a separate credential type from the admin login above, but stored the
  // same way (localStorage "deco_user" + this same `user` state) so the
  // rest of the app (ProtectedRoute, Topbar, Sidebar) doesn't need two
  // parallel session concepts. `role: "client"` plus the license's
  // assigned cameras/features is what Sidebar/LiveFeed/Topbar use to scope
  // what a client can see — see Sidebar.jsx and LiveFeed.jsx.
  const loginAsClient = useCallback(async (username, password) => {
    const res = await api.clientLogin(username, password);
    const clientUser = clientUserFrom(res);
    localStorage.setItem("deco_token", res.session_token);
    localStorage.setItem("deco_user", JSON.stringify(clientUser));
    setUser(clientUser);
    return clientUser;
  }, []);

  const logout = useCallback(() => {
    // Best-effort server-side revoke — the local session clears either
    // way, but this stops the token from working if it leaked (e.g. was
    // logged, or the tab is shared) rather than just forgetting it here.
    const current = userRef.current;
    const done = current?.role === "client" ? api.clientLogout() : current ? api.logoutAdmin() : Promise.resolve();
    localStorage.removeItem("deco_token");
    localStorage.removeItem("deco_user");
    applyLicense(null);
    setUser(null);
    // A client who came from the Master Portal goes back to its sign-in page
    // (after the revoke has been sent, so the session really ends).
    if (current?.sso && current.loginUrl) Promise.resolve(done).finally(() => window.location.assign(current.loginUrl));
  }, []);

  // Merges profile edits (Settings > Profile) into the logged-in user so the
  // Topbar/sidebar avatar and name reflect a save immediately, without
  // requiring a fresh login or page reload.
  const updateUser = useCallback((patch) => {
    setUser((prev) => {
      const next = { ...prev, ...patch };
      localStorage.setItem("deco_user", JSON.stringify(next));
      return next;
    });
  }, []);

  useEffect(() => {
    if (user?.role !== "client") return undefined;
    let cancelled = false;
    async function refresh() {
      try {
        const res = await api.clientMe();
        if (cancelled) return;
        updateUser(clientUserFrom(res));
      } catch {
        // A 401/403 here already triggered client.js's handleAuthFailure()
        // (clears storage + redirects) — nothing further to do.
      }
    }
    const interval = setVisibleInterval(refresh, CLIENT_SESSION_REFRESH_MS);
    return () => {
      cancelled = true;
      clearVisibleInterval(interval);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only re-arm when switching between client/non-client, not on every user-field change
  }, [user?.role]);

  // Operator sessions (local admin or Master Portal sign-in): keep the license
  // summary current. A 401 here (license suspended, session ended) is handled
  // by client.js, which clears the session and goes back to sign-in.
  const loggedIn = !!user && user.role !== "client";
  useEffect(() => {
    if (!loggedIn) return undefined;
    let cancelled = false;
    async function refresh() {
      try {
        const me = await api.getMe();
        if (cancelled) return;
        applyLicense(me.license);
        updateUser({
          license: me.license,
          ...(me.license?.mode === "master" ? { loginUrl: me.license?.login_url || "", viaPortal: true } : {}),
        });
      } catch {
        // offline / 401 already handled
      }
    }
    refresh();
    const interval = setVisibleInterval(refresh, LICENSE_REFRESH_MS);
    return () => {
      cancelled = true;
      clearVisibleInterval(interval);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-arm only on sign-in/out
  }, [loggedIn]);

  return (
    <AuthContext.Provider value={{ user, login, loginAsClient, logout, updateUser }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
