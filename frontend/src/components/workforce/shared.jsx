// Small helpers shared by the Attendance page and its management panels.

export function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function shiftIso(iso, days) {
  const d = new Date(`${iso}T12:00:00`);
  d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—");

export function dur(seconds) {
  if (!seconds || seconds < 60) return "—";
  const m = Math.round(seconds / 60);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

const STATUS_BADGE = {
  Present: "badge-success",
  Late: "badge-warning",
  "Half day": "badge-warning",
  Absent: "badge-danger",
  "On leave": "badge-neutral",
  WFH: "badge-success",
  Holiday: "badge-neutral",
  "Weekly off": "badge-neutral",
  "Not in yet": "badge-neutral",
};

export function DayBadge({ status }) {
  return <span className={`badge ${STATUS_BADGE[status] || "badge-neutral"}`}>{status}</span>;
}
