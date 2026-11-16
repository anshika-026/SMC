// Where this frontend is served and where it keeps its session.
//
// Base path: "/" when SMC has its own host (e.g. the laptop), or a path under
// another site, e.g. "/smc/" on tatv.dewin.in (VITE_BASE_PATH -> vite.config
// `base` -> import.meta.env.BASE_URL). Links inside the router follow it on
// their own (BrowserRouter basename); raw redirects and static files use appPath().
export const APP_BASE = import.meta.env.BASE_URL.replace(/\/$/, "");
export const appPath = (path) => `${APP_BASE}${path}`;

// Session keys. SMC's own names: served under tatv.dewin.in, the Deco Vision
// portal on the same origin keeps ITS session as deco_token / deco_user, and the
// two applications must never read or clear each other's.
export const TOKEN_KEY = "smc_token";
export const USER_KEY = "smc_user";

// Earlier versions stored SMC's session as deco_token / deco_user. Carry it over
// once so nobody is signed out, but only when SMC has its own host: under
// another site those names belong to that site.
function adoptLegacySession() {
  if (APP_BASE !== "") return;
  try {
    if (localStorage.getItem(TOKEN_KEY) === null && localStorage.getItem("deco_token") !== null) {
      localStorage.setItem(TOKEN_KEY, localStorage.getItem("deco_token"));
      const user = localStorage.getItem("deco_user");
      if (user !== null) localStorage.setItem(USER_KEY, user);
    }
    localStorage.removeItem("deco_token");
    localStorage.removeItem("deco_user");
  } catch {
    // storage unavailable: nothing to carry over
  }
}
adoptLegacySession();
