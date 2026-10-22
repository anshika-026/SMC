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
  "intrusion",
  "staff_count",
  "expression",
  "appearance_handoff",
  "object_detection",
]);

const ENABLED = new Set(
  (import.meta.env.VITE_ENABLED_FEATURES || "")
    .split(",")
    .map((f) => f.trim())
    .filter(Boolean)
);

export function featureOn(key) {
  if (!key || !AI_FEATURES.has(key)) return true;
  return ENABLED.size === 0 || ENABLED.has(key);
}
