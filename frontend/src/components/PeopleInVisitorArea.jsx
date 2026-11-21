import { useCallback, useEffect, useMemo, useState } from "react";
import { Film, Images } from "lucide-react";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";
import Modal from "./Modal";
import { fmtDateTime, fmtTime } from "./VisitorAlerts";
import { FacesSeen, RANGES, fmtDuration } from "./VisitorHistory";
import { Snapshot } from "./VisitorEmployees";
import { todayIso } from "./workforce/shared";

const REFRESH_MS = 15_000;
const EMPLOYEE_LIMIT = 500; // the most /visitors/employee-visits returns
const SHOW = [
  ["all", "All"],
  ["employee", "Employees"],
  ["visitor", "Visitors"],
];

// Local calendar day (YYYY-MM-DD) of a unix time in seconds.
function dayOf(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// Everyone who sat in a visitor area, in one list, newest first:
//   visitors   people who are not employees (visitors.py visits; 10 s clip). Sittings that
//              were an employee's are left out by the backend, so nobody shows twice.
//   employees  recognised by face (visitor_area_employee_log; face snapshots)
// Both are shown by default; the All / Employees / Visitors filter narrows it.
export default function PeopleInVisitorArea({ cameras, onOpenEvent, initialShow = "all" }) {
  const [visits, setVisits] = useState(null);
  const [stays, setStays] = useState(null);
  const [error, setError] = useState("");
  const [show, setShow] = useState(initialShow);
  const [cameraId, setCameraId] = useState(0);
  const [range, setRange] = useState("today");
  const [from, setFrom] = useState(todayIso());
  const [to, setTo] = useState(todayIso());
  const [openStay, setOpenStay] = useState(null);
  const [stayShots, setStayShots] = useState(null);

  const load = useCallback(() => {
    return Promise.all([
      api.getVisitorVisits({ cameraId, limit: 2000, start: from, end: to }),
      api.getVisitorAreaEmployeeVisits({ cameraId, limit: EMPLOYEE_LIMIT }),
    ])
      .then(([v, e]) => {
        setVisits(v.visits || []);
        setStays(e.visits || []);
        setError("");
      })
      .catch((e) => setError(e.message));
  }, [cameraId, from, to]);

  useEffect(() => {
    load();
    const h = setVisibleInterval(load, REFRESH_MS);
    return () => clearVisibleInterval(h);
  }, [load]);

  function pick(key) {
    const [f, t] = RANGES.find(([k]) => k === key)[2]();
    setRange(key);
    setFrom(f);
    setTo(t);
  }

  function openSnapshots(r) {
    setOpenStay(r);
    setStayShots(null);
    api
      .getEmployeeVisitSnapshots(r.id)
      .then((d) => setStayShots(d.snapshots || []))
      .catch(() => setStayShots([]));
  }

  const rows = useMemo(() => {
    if (!visits || !stays) return null;
    const inRange = (ts) => {
      const d = dayOf(ts);
      return (!from || d >= from) && (!to || d <= to);
    };
    const v = visits.map((r) => ({ kind: "visitor", key: `v${r.visit}`, arrived: r.arrived_at, r }));
    // The employee list has no date filter on the server, so it is narrowed here.
    const e = stays.filter((r) => inRange(r.arrived_at)).map((r) => ({ kind: "employee", key: `e${r.id}`, arrived: r.arrived_at, r }));
    return [...v, ...e].sort((a, b) => b.arrived - a.arrived);
  }, [visits, stays, from, to]);

  const counts = rows ? { all: rows.length, employee: rows.filter((x) => x.kind === "employee").length, visitor: rows.filter((x) => x.kind === "visitor").length } : null;
  const shown = rows ? rows.filter((x) => show === "all" || x.kind === show) : null;
  const insideNow = rows ? rows.filter((x) => x.r.status === "inside").length : 0;

  return (
    <div className="card p-4">
      <div className="flex items-start justify-between gap-2 mb-3 flex-wrap">
        <div>
          <h3 className="text-sm font-semibold text-ink-900">People in visitor area</h3>
          <p className="text-xs text-slate-500">
            Everyone who sat in a visitor area: visitors (not recognised, with a 10 s clip) and employees (recognised by face, with snapshots).
          </p>
        </div>
        <div className="flex items-center gap-2">
          {rows && (
            <span className={`badge ${insideNow ? "badge-warning" : "badge-neutral"}`}>{insideNow} in visitor area now</span>
          )}
          <select className="input-field !w-auto text-xs" value={cameraId || ""} onChange={(e) => setCameraId(e.target.value ? Number(e.target.value) : 0)}>
            <option value="">All cameras</option>
            {cameras.map((c) => (
              <option key={c.camera_id} value={c.camera_id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 mb-3">
        <div className="inline-flex rounded-full bg-slate-100 p-0.5" role="group" aria-label="Show">
          {SHOW.map(([k, label]) => (
            <button key={k} type="button" onClick={() => setShow(k)}
              className={`px-3 py-1 rounded-full text-xs font-medium ${show === k ? "bg-white text-ink-900 shadow-sm" : "text-slate-500 hover:text-ink-900"}`}>
              {label}{counts ? ` (${counts[k]})` : ""}
            </button>
          ))}
        </div>
        <span className="w-px h-5 bg-border-200 mx-1 hidden sm:inline-block" />
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

      {error && <p className="text-xs text-danger-600 mb-2">Could not load: {error}</p>}
      {!rows && !error && <p className="text-sm text-slate-400 py-6 text-center">Loading…</p>}
      {shown && shown.length === 0 && (
        <p className="text-sm text-slate-400 py-6 text-center">
          {show === "employee" ? "No employees" : show === "visitor" ? "No visitors" : "Nobody"} in a visitor area {from || to ? "in this period" : "yet"}.
        </p>
      )}
      {shown && shown.length > 0 && (
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Type</th>
                <th>Who</th>
                <th>Camera</th>
                <th>Arrived</th>
                <th>Last seen</th>
                <th>Stayed</th>
                <th>Status</th>
                <th>Clip / photos</th>
              </tr>
            </thead>
            <tbody>
              {shown.map(({ kind, key, r }) => {
                const employee = kind === "employee";
                const end = r.status === "inside" ? r.last_seen : r.left_at || r.last_seen;
                return (
                  <tr key={key}>
                    <td>
                      <span className={`badge ${employee ? "badge-success" : "badge-warning"}`}>{employee ? "Employee" : "Visitor"}</span>
                    </td>
                    <td>
                      {employee ? (
                        <div className="flex items-center gap-2">
                          {r.snapshot_id && <Snapshot captureId={r.snapshot_id} size={32} onClick={() => openSnapshots(r)} />}
                          <div>
                            <div className="font-medium text-ink-900">{r.name}</div>
                            <div className="text-[11px] text-slate-400">ID {r.employee_id}{r.department ? ` · ${r.department}` : ""}</div>
                          </div>
                        </div>
                      ) : (
                        <FacesSeen v={r} />
                      )}
                    </td>
                    <td>{r.camera_name}</td>
                    <td>{fmtDateTime(r.arrived_at)}</td>
                    <td>{r.last_seen ? fmtTime(r.last_seen) : "-"}</td>
                    <td>{fmtDuration(employee ? end - r.arrived_at : r.duration_seconds)}</td>
                    <td>
                      <span className={`badge ${r.status === "inside" ? "badge-warning" : "badge-neutral"}`}>
                        {r.status === "inside" ? "Inside now" : `Left ${fmtTime(r.left_at)}`}
                      </span>
                    </td>
                    <td>
                      {employee ? (
                        r.snapshot_id ? (
                          <button type="button" className="text-xs text-brand-600 hover:underline inline-flex items-center gap-1" onClick={() => openSnapshots(r)}>
                            <Images size={12} /> {r.snapshot_count > 1 ? `${r.snapshot_count} photos` : "Photo"}
                          </button>
                        ) : (
                          <span className="text-xs text-slate-400">No snapshot</span>
                        )
                      ) : r.latest_event_id ? (
                        <button type="button" className="text-xs text-brand-600 hover:underline inline-flex items-center gap-1" onClick={() => onOpenEvent(r.latest_event_id)}>
                          <Film size={12} /> View clip
                        </button>
                      ) : (
                        <span className="text-xs text-slate-400" title="Clips are deleted automatically after their retention period">Expired</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <Modal open={!!openStay} onClose={() => setOpenStay(null)} title={openStay ? `${openStay.name} · ${openStay.camera_name}` : ""} width="max-w-2xl">
        {openStay && (
          <div className="space-y-3">
            <p className="text-xs text-slate-500">
              Employee · {fmtDateTime(openStay.arrived_at)} to {fmtTime(openStay.status === "inside" ? openStay.last_seen : openStay.left_at)}. Face captures stored during this stay.
            </p>
            {!stayShots && <p className="text-sm text-slate-400 py-6 text-center">Loading…</p>}
            {stayShots && stayShots.length === 0 && <p className="text-sm text-slate-400 py-6 text-center">No snapshots stored for this stay.</p>}
            {stayShots && stayShots.length > 0 && (
              <div className="grid grid-cols-3 sm:grid-cols-4 gap-3">
                {stayShots.map((s) => (
                  <figure key={s.capture_id} className="text-center">
                    <Snapshot captureId={s.capture_id} size={120} />
                    <figcaption className="text-[11px] text-slate-500 mt-1">{fmtTime(s.captured_at)}</figcaption>
                  </figure>
                ))}
              </div>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
