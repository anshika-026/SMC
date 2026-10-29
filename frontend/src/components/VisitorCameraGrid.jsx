import { Video, VideoOff, ScanEye, Users } from "lucide-react";
import LiveCameraTile from "./LiveCameraTile";

// Live camera object for useLiveCameraFeed: the real one from /cameras when
// available, otherwise just the id and name the visitors API gave us.
export function liveCameraFor(vc, cameras) {
  const real = cameras.find((c) => c.id === vc.camera_id);
  if (real) return real;
  return {
    id: vc.camera_id,
    code: `CAM-${vc.camera_id}`,
    label: vc.name,
    site: vc.site,
    isConfigured: true,
    feedOn: true,
  };
}

export function CameraStatusBadges({ vc }) {
  const detectionLabel = !vc.zone ? "No area set" : vc.detection_active ? "Detection active" : "Detection inactive";
  const detectionOk = !!vc.zone && vc.detection_active;
  return (
    <div className="flex flex-wrap gap-1.5">
      <span className={`badge ${vc.online ? "badge-success" : "badge-danger"}`}>
        {vc.online ? <Video size={11} /> : <VideoOff size={11} />}
        {vc.online ? "Camera online" : "Camera offline"}
      </span>
      <span className={`badge ${detectionOk ? "badge-success" : "badge-neutral"}`}>
        <ScanEye size={11} />
        {detectionLabel}
      </span>
      {vc.inside_now > 0 && (
        <span className="badge badge-warning">
          <Users size={11} />
          {vc.inside_now} visitor{vc.inside_now === 1 ? "" : "s"} inside now
        </span>
      )}
      {vc.zone && vc.detection_active && vc.employee_exclusion === false && (
        <span className="badge badge-neutral" title="Face recognition is not running on this camera, so employees sitting here are alerted as visitors">
          Employees not excluded here
        </span>
      )}
    </div>
  );
}

export default function VisitorCameraGrid({ items, cameras, onOpen }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {items.map((vc) => (
        <div key={vc.camera_id} className="space-y-2">
          <LiveCameraTile camera={liveCameraFor(vc, cameras)} overlay={false} onClick={() => onOpen(vc.camera_id)} />
          <div className="px-1 flex items-center justify-between gap-2 flex-wrap">
            <CameraStatusBadges vc={vc} />
            <button type="button" className="text-xs text-brand-600 hover:underline" onClick={() => onOpen(vc.camera_id)}>
              {vc.zone ? "View / edit area" : "Set detection area"}
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
