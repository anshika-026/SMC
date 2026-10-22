import { useEffect, useState } from "react";
import Modal from "./Modal";
import ZoneEditor from "./ZoneEditor";
import * as api from "../api/client";

// Footfall > Add camera: pick a camera, drag a box over the area to count in,
// save. The camera becomes a footfall gate and starts counting straight away
// (backend/app/footfall_routes.py). Opened with `initialCameraId` it edits that
// camera's area instead.
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
  const gate = camera ? { camera_id: camera.id, name: camera.label, zone: counting.get(camera.id)?.zone || null } : null;

  return (
    <Modal open={open} onClose={onClose} title={initialCameraId ? "Edit counting area" : "Add camera to footfall"} width="max-w-3xl">
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
          <>
            <p className="text-sm text-slate-500">
              Drag a box over the area to count in. Only people inside it are counted. Saving adds this camera to footfall and
              starts counting.
            </p>
            <ZoneEditor
              key={gate.camera_id}
              gate={gate}
              saveLabel="Save area & start counting"
              savedMessage="Saved. Footfall is now counting in this area."
              onSave={(roi) => api.addFootfallCamera(gate.camera_id, roi)}
              onSaved={() => onDone?.()}
            />
          </>
        ) : (
          !error && <p className="text-sm text-slate-400">Pick a camera to mark its counting area.</p>
        )}
      </div>
    </Modal>
  );
}
