import { useCallback, useEffect, useState } from "react";
import { Film } from "lucide-react";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";
import { fmtDateTime, fmtTime } from "./VisitorAlerts";

const REFRESH_MS = 15_000;

export function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

// Visitor history: one row per visit (a person's continuous stay), so repeat
// alerts of the same stay never show as extra visitors. Employees are never
// in here (see VisitorEmployees). Kept after the clips are deleted.
export default function VisitorHistory({ cameras, onOpenEvent }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState("");
  const [cameraId, setCameraId] = useState(0);

  const load = useCallback(() => {
    return api
      .getVisitorVisits({ cameraId, limit: 200 })
      .then((d) => {
        setRows(d.visits || []);
        setError("");
      })
      .catch((e) => setError(e.message));
  }, [cameraId]);

  useEffect(() => {
    load();
    const h = setVisibleInterval(load, REFRESH_MS);
    return () => clearVisibleInterval(h);
  }, [load]);

  return (
    <div className="card p-4">
      <div className="flex items-center justify-between gap-2 mb-3 flex-wrap">
        <div>
          <h3 className="text-sm font-semibold text-ink-900">Visitor history</h3>
          <p className="text-xs text-slate-500">One row per visit. Registered employees are not included.</p>
        </div>
        <select className="input-field !w-auto text-xs" value={cameraId || ""} onChange={(e) => setCameraId(e.target.value ? Number(e.target.value) : 0)}>
          <option value="">All cameras</option>
          {cameras.map((c) => (
            <option key={c.camera_id} value={c.camera_id}>
              {c.name}
            </option>
          ))}
        </select>
      </div>
      {error && <p className="text-xs text-danger-600 mb-2">Could not load visitor history: {error}</p>}
      {!rows && !error && <p className="text-sm text-slate-400 py-6 text-center">Loading visitor history…</p>}
      {rows && rows.length === 0 && <p className="text-sm text-slate-400 py-6 text-center">No visitor visits recorded yet.</p>}
      {rows && rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Arrived</th>
                <th>Camera</th>
                <th>Last seen</th>
                <th>Stayed</th>
                <th>Status</th>
                <th>Alerts</th>
                <th>Clip</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((v) => (
                <tr key={v.visit}>
                  <td>{fmtDateTime(v.arrived_at)}</td>
                  <td>{v.camera_name}</td>
                  <td>{v.last_seen ? fmtTime(v.last_seen) : "-"}</td>
                  <td>{fmtDuration(v.duration_seconds)}</td>
                  <td>
                    <span className={`badge ${v.status === "inside" ? "badge-warning" : "badge-neutral"}`}>
                      {v.status === "inside" ? "Inside now" : `Left ${fmtTime(v.left_at)}`}
                    </span>
                  </td>
                  <td title={v.alerts > 1 ? "First alert, then one every 30 minutes while they stayed" : undefined}>{v.alerts}</td>
                  <td>
                    {v.latest_event_id ? (
                      <button type="button" className="text-xs text-brand-600 hover:underline inline-flex items-center gap-1" onClick={() => onOpenEvent(v.latest_event_id)}>
                        <Film size={12} /> View
                      </button>
                    ) : (
                      <span className="text-xs text-slate-400" title="Clips are deleted automatically after their retention period">Expired</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
