import { useCallback, useEffect, useState } from "react";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";
import { fmtDateTime, fmtTime } from "./VisitorAlerts";
import { fmtDuration } from "./VisitorHistory";

const SEEN_REFRESH_MS = 15_000;

// Employees recognised by face in visitor areas: kept apart from visitor
// records and never alerted as visitors.
export default function VisitorEmployees() {
  const [seen, setSeen] = useState(null);
  const [seenError, setSeenError] = useState("");

  const loadSeen = useCallback(() => {
    return api
      .getVisitorAreaEmployeeVisits({ limit: 100 })
      .then((d) => {
        setSeen(d.visits || []);
        setSeenError("");
      })
      .catch((e) => setSeenError(e.message));
  }, []);

  useEffect(() => {
    loadSeen();
    const h = setVisibleInterval(loadSeen, SEEN_REFRESH_MS);
    return () => clearVisibleInterval(h);
  }, [loadSeen]);

  return (
    <div className="card p-4">
      <h3 className="text-sm font-semibold text-ink-900">Employees seen in visitor areas</h3>
      <p className="text-xs text-slate-500 mb-3">Kept separately from visitor records; these never raised an alert.</p>
      {seenError && <p className="text-xs text-danger-600 mb-2">Could not load: {seenError}</p>}
      {!seen && !seenError && <p className="text-sm text-slate-400 py-6 text-center">Loading…</p>}
      {seen && seen.length === 0 && <p className="text-sm text-slate-400 py-6 text-center">No employees seen in a visitor area yet.</p>}
      {seen && seen.length > 0 && (
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Employee</th>
                <th>Camera</th>
                <th>Arrived</th>
                <th>Last seen</th>
                <th>Stayed</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {seen.map((r) => (
                <tr key={r.id}>
                  <td>{r.name}</td>
                  <td>{r.camera_name}</td>
                  <td>{fmtDateTime(r.arrived_at)}</td>
                  <td>{fmtTime(r.last_seen)}</td>
                  <td>{fmtDuration(r.last_seen - r.arrived_at)}</td>
                  <td>
                    <span className={`badge ${r.status === "inside" ? "badge-success" : "badge-neutral"}`}>
                      {r.status === "inside" ? "In area" : `Left ${fmtTime(r.left_at)}`}
                    </span>
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
