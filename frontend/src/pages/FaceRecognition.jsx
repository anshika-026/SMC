import { useEffect, useState } from "react";
import PageHeader from "../components/PageHeader";
import StatCard from "../components/StatCard";
import LiveCameraTile from "../components/LiveCameraTile";
import CameraViewerModal from "../components/CameraViewerModal";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";

// Every camera added in Camera Management, live, with the face-recognition
// overlay (blue boxes; a name only once confidently recognised, otherwise
// "Person"), plus the status of the face-image collection session.

const REFRESH_MS = 15_000;

function formatBytes(n) {
  if (!n) return "0 MB";
  return n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB` : `${(n / 1e6).toFixed(1)} MB`;
}

function formatRemaining(seconds) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  return d > 0 ? `${d}d ${h}h left` : `${h}h left`;
}

export default function FaceRecognition() {
  const [cameras, setCameras] = useState(null);
  const [collection, setCollection] = useState(null);
  const [expanded, setExpanded] = useState(null);

  useEffect(() => {
    api.getCameras().then(setCameras).catch(() => setCameras([]));
  }, []);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .getCollectionStatus()
        .then((d) => !cancelled && setCollection(d))
        .catch(() => {}); // admin-only endpoint; a client login just doesn't see the strip
    load();
    const id = setVisibleInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      clearVisibleInterval(id);
    };
  }, []);

  const session = collection?.session;
  const perCamera = collection?.per_camera || {};

  return (
    <div className="space-y-5">
      <PageHeader title="Face Recognition" />

      {collection && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <StatCard
            label="Image Collection"
            value={session ? "Running" : "Not running"}
            sub={session ? formatRemaining(session.time_remaining_seconds) : "start it from the API when ready"}
            subTone="neutral"
          />
          <StatCard
            label="Face Images Captured"
            value={collection.current_total}
            sub={`limit ${collection.capture_limit}`}
            subTone="neutral"
          />
          <StatCard
            label="Collecting Cameras"
            value={collection.active_camera_ids.length}
            sub={`of ${cameras?.length ?? 0} cameras`}
            subTone="neutral"
          />
          <StatCard label="Storage Used" value={formatBytes(collection.disk_usage_bytes)} sub="pending face crops" subTone="neutral" />
        </div>
      )}

      {cameras === null ? (
        <p className="text-sm text-slate-400">Loading cameras…</p>
      ) : cameras.length === 0 ? (
        <div className="card p-10 text-center text-sm text-slate-400">No cameras yet — add one from Camera Management.</div>
      ) : (
        <div className="grid sm:grid-cols-2 xl:grid-cols-3 gap-4">
          {cameras.map((cam) => {
            const pc = perCamera[cam.id];
            return (
              <div key={cam.id} className="space-y-1.5">
                <LiveCameraTile camera={cam} faceView onClick={() => setExpanded(cam)} />
                {pc && (
                  <p className="text-xs text-slate-500 px-1">
                    {pc.collecting ? "Collecting" : "Not collecting"} · {pc.total} face images
                    {pc.health === "stalled" ? " · no new images (stalled)" : ""}
                  </p>
                )}
              </div>
            );
          })}
        </div>
      )}

      <CameraViewerModal camera={expanded} faceView onClose={() => setExpanded(null)} />
    </div>
  );
}
