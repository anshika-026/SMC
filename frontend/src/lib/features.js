// AI features this deployment offers, from VITE_ENABLED_FEATURES (comma-
// separated keys of backend/app/analytics_settings.py FEATURES; must match
// the backend's ENABLED_FEATURES). Empty = everything. This deployment sets
// "footfall", so every other AI page, card and setting is hidden.
// Non-AI parts (dashboard, cameras, sites, alerts, settings) always show.
const AI_FEATURES = new Set([
  "face_recognition",
  "desk_analytics",
  "footfall",
  "visitors",
  "live_overlay",
  "staff_count",
  "expression",
  "appearance_handoff",
  "intrusion",
]);

const ENABLED = new Set(
  (import.meta.env.VITE_ENABLED_FEATURES || "")
    .split(",")
    .map((f) => f.trim())
    .filter(Boolean)
);

// When SMC is connected to the Deco Vision Master Portal, the license's
// features (Master Portal keys, from /api/auth/me) also decide what shows.
// null = not connected: nothing extra is hidden. The backend enforces the
// same rules (backend/app/master_link.py); this only keeps the menus honest.
let LICENSED = null;
export function setLicensedFeatures(keys) {
  LICENSED = Array.isArray(keys) ? new Set(keys) : null;
}

// Processing switch -> the license feature it needs (master_link.ANALYTICS_LICENSE_FEATURE).
const SWITCH_LICENSE = {
  face_recognition: "face_recognition",
  appearance_handoff: "face_recognition",
  intrusion: "intrusion_detection",
  desk_analytics: "workforce_analytics",
  footfall: "footfall_analytics",
  visitors: "visitor_management",
  staff_count: "attendance",
};

// Does the Master Portal license include this feature (its own key, e.g.
// "attendance", "face_training")? Always true when not connected.
export function licenseOn(key) {
  return !key || LICENSED === null || LICENSED.has(key);
}

export function featureOn(key) {
  if (!key || !AI_FEATURES.has(key)) return true;
  return (ENABLED.size === 0 || ENABLED.has(key)) && licenseOn(SWITCH_LICENSE[key]);
}
