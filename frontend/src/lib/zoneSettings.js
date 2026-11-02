// Ported from MAINDV (intrusion-loitering). Pure helpers for the zone type / duration settings (pages/Intrusion.jsx). No React and no browser
// APIs, so they are unit-tested with `npm test`.

// Shortest and longest "stay at least this long" a loitering zone accepts.
// Keep in step with LOITER_MIN_SECONDS / LOITER_MAX_SECONDS in backend/app/intrusion.py.
export const LOITER_MIN_SECONDS = 5;
export const LOITER_MAX_SECONDS = 24 * 3600;

export const ZONE_TYPES = [
  { value: "restricted", label: "Restricted area: alert when someone is inside" },
  { value: "loitering", label: "Loitering: alert when someone stays too long" },
];

// The form holds minutes (what people think in); the server wants whole seconds.
export function minutesToSeconds(minutes) {
  if (minutes === "" || minutes === null || minutes === undefined) return NaN;
  const n = Number(minutes);
  return Number.isFinite(n) ? Math.round(n * 60) : NaN;
}

export function secondsToMinutes(seconds) {
  return seconds == null ? "" : String(+(seconds / 60).toFixed(2));
}

// "" when the duration can be saved, otherwise one sentence saying what is wrong.
export function dwellProblem(seconds) {
  if (!Number.isFinite(seconds)) return "Enter how long a person must stay, in minutes.";
  if (seconds < LOITER_MIN_SECONDS) return "The shortest duration is 5 seconds (0.1 minutes).";
  if (seconds > LOITER_MAX_SECONDS) return "The longest duration is 24 hours (1440 minutes).";
  return "";
}

// 900 -> "15 minutes", 90 -> "1 min 30 s", 3600 -> "1 hour". Same wording as the backend alert message.
export function formatDuration(seconds) {
  const s = Math.round(seconds);
  if (s >= 3600 && s % 3600 === 0) return `${s / 3600} hour${s / 3600 !== 1 ? "s" : ""}`;
  if (s >= 60 && s % 60 === 0) return `${s / 60} minute${s / 60 !== 1 ? "s" : ""}`;
  if (s >= 60) return `${Math.floor(s / 60)} min ${s % 60} s`;
  return `${s} second${s !== 1 ? "s" : ""}`;
}

// What the zone table shows for a zone's type.
export function zoneTypeLabel(zone) {
  return zone.zone_type === "loitering" ? `Loitering · ${formatDuration(zone.dwell_seconds || 0)}` : "Restricted";
}

// What the zone table shows for a zone's alert setting.
export function zoneAlertLabel(zone) {
  if (zone.alerts_enabled === false) return "Alerts off";
  return zone.severity || (zone.zone_type === "loitering" ? "Medium" : "High");
}
