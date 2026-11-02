import { useCallback, useEffect, useState } from "react";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";
import Modal from "./Modal";
import { fmtDateTime, fmtTime } from "./VisitorAlerts";
import { fmtDuration } from "./VisitorHistory";

const SEEN_REFRESH_MS = 15_000;
// Every stay the log holds is listed (history), newest first.
const HISTORY_LIMIT = 300;

// One stored face capture (backend/app/visitor_routes.py employee-snapshots):
// fetched with the session token, which a plain <img src> can't send.
function Snapshot({ captureId, size = 40, onClick }) {
  const [url, setUrl] = useState(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    let objectUrl = null;
    api
      .fetchEmployeeSnapshotObjectUrl(captureId)
      .then((u) => {
        objectUrl = u;
        if (alive) setUrl(u);
        else URL.revokeObjectURL(u);
      })
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [captureId]);
  const box = { width: size, height: size };
  if (failed) return <span style={box} className="inline-block rounded-lg bg-slate-100" title="Snapshot not available" />;
  if (!url) return <span style={box} className="inline-block rounded-lg bg-slate-100 animate-pulse" />;
  return (
    <img
      src={url}
      alt="Employee snapshot"
      style={box}
      className={`rounded-lg object-cover ${onClick ? "cursor-pointer hover:ring-2 hover:ring-brand-400" : ""}`}
      onClick={onClick}
    />
  );
}

// Employees recognised by face in visitor areas: kept apart from visitor
// records and never alerted as visitors.
export default function VisitorEmployees() {
  const [seen, setSeen] = useState(null);
  const [seenError, setSeenError] = useState("");
  const [openStay, setOpenStay] = useState(null); // a row of `seen`
  const [stayShots, setStayShots] = useState(null);

  const loadSeen = useCallback(() => {
    return api
      .getVisitorAreaEmployeeVisits({ limit: HISTORY_LIMIT })
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

  function openSnapshots(r) {
    setOpenStay(r);
    setStayShots(null);
    api
      .getEmployeeVisitSnapshots(r.id)
      .then((d) => setStayShots(d.snapshots || []))
      .catch(() => setStayShots([]));
  }

  const presentNow = seen ? seen.filter((r) => r.status === "inside").length : 0;

  return (
    <div className="card p-4">
      <div className="flex flex-wrap items-start justify-between gap-2 mb-3">
        <div>
          <h3 className="text-sm font-semibold text-ink-900">Employees seen in visitor areas</h3>
          <p className="text-xs text-slate-500">
            Recognised employees, kept separately from visitor records; these never raised an alert and are not counted as visitors.
          </p>
        </div>
        {seen && (
          <span className={`badge ${presentNow ? "badge-success" : "badge-neutral"}`}>
            {presentNow} present in visitor area now
          </span>
        )}
      </div>
      {seenError && <p className="text-xs text-danger-600 mb-2">Could not load: {seenError}</p>}
      {!seen && !seenError && <p className="text-sm text-slate-400 py-6 text-center">Loading…</p>}
      {seen && seen.length === 0 && <p className="text-sm text-slate-400 py-6 text-center">No employees seen in a visitor area yet.</p>}
      {seen && seen.length > 0 && (
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Snapshot</th>
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
                  <td>
                    {r.snapshot_id ? (
                      <span className="inline-flex items-center gap-1.5">
                        <Snapshot captureId={r.snapshot_id} onClick={() => openSnapshots(r)} />
                        {r.snapshot_count > 1 && <span className="text-[11px] text-slate-400">+{r.snapshot_count - 1}</span>}
                      </span>
                    ) : (
                      <span className="text-[11px] text-slate-400">No snapshot</span>
                    )}
                  </td>
                  <td>
                    <div className="flex items-center gap-1.5">
                      <span className="font-medium text-ink-900">{r.name}</span>
                      <span className="badge badge-neutral text-[10px]">Employee</span>
                    </div>
                    <div className="text-[11px] text-slate-400">
                      ID {r.employee_id}
                      {r.department ? ` · ${r.department}` : ""}
                    </div>
                  </td>
                  <td>{r.camera_name}</td>
                  <td>{fmtDateTime(r.arrived_at)}</td>
                  <td>{fmtTime(r.last_seen)}</td>
                  <td>{fmtDuration((r.status === "inside" ? r.last_seen : r.left_at || r.last_seen) - r.arrived_at)}</td>
                  <td>
                    <span className={`badge ${r.status === "inside" ? "badge-success" : "badge-neutral"}`}>
                      {r.status === "inside" ? "Present in Visitor Area" : `Left ${fmtTime(r.left_at)}`}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Modal open={!!openStay} onClose={() => setOpenStay(null)} title={openStay ? `${openStay.name} · ${openStay.camera_name}` : ""} width="max-w-2xl">
        {openStay && (
          <div className="space-y-3">
            <p className="text-xs text-slate-500">
              Employee · {fmtDateTime(openStay.arrived_at)} to {fmtTime(openStay.status === "inside" ? openStay.last_seen : openStay.left_at)}. Original
              face captures stored during this stay.
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
