// ---------------------------------------------------------------------------
// API CLIENT — unique footfall (backend/app/footfall_routes.py)
// Every page reads backend data through the functions below. There is no
// mock fallback: if the backend fails, the page shows the error.
// ---------------------------------------------------------------------------

// Base URL of the backend API. Production builds use the relative "/api"
// (backend serves the built SPA on the same origin, or nginx proxies /api);
// see frontend/.env.example for local development.
export const BASE_URL = (import.meta.env.VITE_API_BASE_URL || "/api").replace(/\/$/, "");

// Matches the backend's optional API_TOKEN. Leave unset when the backend
// runs without one.
const API_TOKEN = import.meta.env.VITE_API_TOKEN || "";

function authHeaders() {
  return API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {};
}

async function request(path, options = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const err = new Error(body?.detail || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// ---- Footfall ------------------------------------------------------------
// Unique people across both cameras — each person counted once no matter
// which camera(s) saw them.
export async function getFootfallSummary() {
  return request("/footfall/summary");
}
// UAT panel: every unique person currently counted, and a one-click restart.
export async function getFootfallPeople() {
  return request("/footfall/people");
}
export async function resetFootfall() {
  return request("/footfall/reset", { method: "POST" });
}
// Counting zone per camera: polygon [[x, y], ...] in 0..1 frame fractions,
// or null for the whole frame.
export async function setFootfallZone(cameraId, roi) {
  return request(`/footfall/cameras/${cameraId}/zone`, { method: "PUT", body: JSON.stringify({ roi }) });
}
export async function fetchFootfallFrameObjectUrl(cameraId) {
  const res = await fetch(`${BASE_URL}/footfall/cameras/${cameraId}/frame`, { headers: { ...authHeaders() } });
  if (!res.ok) throw new Error(`No frame from this camera yet (${res.status})`);
  return URL.createObjectURL(await res.blob());
}
// Fetched (not a plain <img src>) so the optional Bearer token is sent.
export async function fetchFootfallSnapshotObjectUrl(snapshotId) {
  const res = await fetch(`${BASE_URL}/footfall/snapshots/${snapshotId}`, { headers: { ...authHeaders() } });
  if (!res.ok) throw new Error(`Could not load snapshot (${res.status})`);
  return URL.createObjectURL(await res.blob());
}
