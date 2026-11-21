// The login ticket from a Deco Vision pass (pages/Sso.jsx -> pages/Login.jsx).
// Kept in sessionStorage: this tab only, gone when it closes, and only for the
// few minutes the backend accepts it.
const KEY = "smc_portal_ticket";

export function savePortalTicket(ticket, expiresInSeconds, company = "") {
  try {
    sessionStorage.setItem(KEY, JSON.stringify({ ticket, company, expiresAt: Date.now() + expiresInSeconds * 1000 }));
  } catch {
    // storage blocked: the login form will ask to open SMC from Deco Vision again
  }
}

export function readPortalTicket() {
  try {
    const t = JSON.parse(sessionStorage.getItem(KEY) || "null");
    if (t?.ticket && t.expiresAt > Date.now()) return t;
  } catch {
    // unreadable: treat as none
  }
  clearPortalTicket();
  return null;
}

export function clearPortalTicket() {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // nothing to clear
  }
}
