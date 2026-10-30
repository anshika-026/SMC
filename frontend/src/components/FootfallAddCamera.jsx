import { useEffect, useState } from "react";
import Modal from "./Modal";
import FootfallGateEditor from "./FootfallGateEditor";
import * as api from "../api/client";

// Footfall > Add camera: pick a camera, draw the ENTRY LINE across its gate
// (and optionally an area around it), save. The camera becomes a footfall gate
// and counts the people who walk in through it (backend/app/footfall.py).
// Opened with `initialCameraId` it edits that gate instead.
export default function FootfallAddCamera({ open, onClose, gates, initialCameraId = null, onDone }) {
  const [cameras, setCameras] = useState([]);
  const [cameraId, setCameraId] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setError("");
    setCameraId(initialCameraId);
    api
      .getCameras()
      .then((cams) => setCameras(cams.filter((c) => c.isConfigured)))
      .catch(() => setError("Couldn't load the cameras. Is the backend running?"));
  }, [open, initialCameraId]);

  if (!open) return null;
  const counting = new Map((gates || []).map((g) => [g.camera_id, g]));
  const camera = cameras.find((c) => c.id === cameraId);
  const existing = camera ? counting.get(camera.id) : null;
  const gate = camera
    ? {
        camera_id: camera.id,
        name: camera.label,
        zone: existing?.zone || null,
        entry_line: existing?.entry_line || null,
        inside_sign: existing?.inside_sign ?? 1,
      }
    : null;

  return (
    <Modal open={open} onClose={onClose} title={initialCameraId ? "Edit gate" : "Add gate camera to footfall"} width="max-w-3xl">
      <div className="space-y-4">
        {!initialCameraId && (
          <div>
            <label htmlFor="ff-camera" className="text-sm font-medium text-ink-900 block mb-1.5">
              Camera
            </label>
            <select
              id="ff-camera"
              value={cameraId ?? ""}
              onChange={(e) => setCameraId(e.target.value ? Number(e.target.value) : null)}
              className="w-full px-3 py-2 rounded-lg border border-border-200 text-sm bg-white"
            >
              <option value="">Choose a camera…</option>
              {cameras.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label} ({c.site}){counting.has(c.id) ? " — already counting" : ""}
                </option>
              ))}
            </select>
          </div>
        )}
        {error && <p className="text-sm text-danger-500">{error}</p>}
        {gate ? (
          <FootfallGateEditor key={gate.camera_id} gate={gate} onSaved={() => onDone?.()} />
        ) : (
          !error && <p className="text-sm text-slate-400">Pick a camera to mark its gate.</p>
        )}
      </div>
    </Modal>
  );
}
