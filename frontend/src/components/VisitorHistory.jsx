import { useCallback, useEffect, useState } from "react";
import { Film } from "lucide-react";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";
import { fmtDateTime, fmtTime } from "./VisitorAlerts";
import { shiftIso, todayIso } from "./workforce/shared";

const REFRESH_MS = 15_000;

// Quick ranges for the calendar; "All time" leaves both dates empty.
const RANGES = [
  ["today", "Today", () => [todayIso(), todayIso()]],
  ["7", "Last 7 days", () => [shiftIso(todayIso(), -6), todayIso()]],
  ["30", "Last 30 days", () => [shiftIso(todayIso(), -29), todayIso()]],
  ["all", "All time", () => ["", ""]],
];

export function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

// Who the camera recognised while a visit lasted (visitors.faces_during_visits): employees, then
// "possible" matches, then the count of faces nobody matched.
function FacesSeen({ f }) {
  if (!f) return <span className="text-xs text-slate-400">-</span>;
  const top = f.employees.slice(0, 2);
  const more = f.employees.length - top.length;
  if (!top.length && !f.possible.length && !f.unknown) return <span className="text-xs text-slate-400" title="The camera captured no face during this visit">No face seen</span>;
  return (
    <div className="flex flex-wrap items-center gap-1 max-w-[260px]">
      {top.map((e) => (
        <span key={e.employee_id} className="badge badge-success" title={`${e.name}: recognised in ${e.faces} face picture(s) on this camera during the visit`}>{e.name}</span>
      ))}
      {more > 0 && <span className="text-[11px] text-slate-500" title={f.employees.slice(2).map((e) => e.name).join(", ")}>+{more}</span>}
      {!top.length && f.possible.slice(0, 1).map((e) => (
        <span key={e.employee_id} className="badge badge-neutral" title={`Possible match, not confirmed (${e.faces} face picture(s))`}>maybe {e.name}</span>
      ))}
      {!top.length && !f.possible.length && <span className="badge badge-warning" title={`${f.unknown} face picture(s), none matched an employee`}>Unknown face</span>}
    </div>
  );
}

// Visitor history: one row per visit (a person's continuous stay), so repeat
// alerts of the same stay never show as extra visitors. Employees are never
// in here (see VisitorEmployees). Kept after the clips are deleted.
export default function VisitorHistory({ cameras, onOpenEvent }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState("");
  const [cameraId, setCameraId] = useState(0);
  const [range, setRange] = useState("all");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");

  const load = useCallback(() => {
    return api
      .getVisitorVisits({ cameraId, limit: 2000, start: from, end: to })
      .then((d) => {
        setRows(d.visits || []);
        setError("");
      })
      .catch((e) => setError(e.message));
  }, [cameraId, from, to]);

  function pick(key) {
    const [f, t] = RANGES.find(([k]) => k === key)[2]();
    setRange(key);
    setFrom(f);
    setTo(t);
  }

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
          <p className="text-xs text-slate-500">
            One row per visit by someone not recognised while seated. "Faces seen" names who the camera recognised during the visit: usually that employee, unrecognised while seated.
            {rows && ` ${rows.length} visit${rows.length === 1 ? "" : "s"}${from || to ? " in this period" : " in total"}.`}
          </p>
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
      <div className="flex flex-wrap items-center gap-2 mb-3">
        {RANGES.map(([k, label]) => (
          <button key={k} type="button" onClick={() => pick(k)}
            className={`px-3 py-1 rounded-full text-xs font-medium ${range === k ? "bg-brand-500 text-white" : "bg-white border border-border-200 text-slate-600 hover:text-ink-900"}`}>
            {label}
          </button>
        ))}
        <span className="flex items-center gap-1.5 text-xs text-slate-500 ml-auto">
          <input type="date" aria-label="From" className="input-field !w-auto !py-1.5 text-xs" value={from} max={to || todayIso()}
            onChange={(e) => { setRange(""); setFrom(e.target.value); }} />
          to
          <input type="date" aria-label="To" className="input-field !w-auto !py-1.5 text-xs" value={to} min={from || undefined} max={todayIso()}
            onChange={(e) => { setRange(""); setTo(e.target.value); }} />
        </span>
      </div>
      {error && <p className="text-xs text-danger-600 mb-2">Could not load visitor history: {error}</p>}
      {!rows && !error && <p className="text-sm text-slate-400 py-6 text-center">Loading visitor history…</p>}
      {rows && rows.length === 0 && (
        <p className="text-sm text-slate-400 py-6 text-center">{from || to ? "No visitor visits in this period." : "No visitor visits recorded yet."}</p>
      )}
      {rows && rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Arrived</th>
                <th title="Faces the camera recognised while this visit lasted (anyone in view, not proof of who sat there)">Faces seen</th>
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
                  <td><FacesSeen f={v.faces_seen} /></td>
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
